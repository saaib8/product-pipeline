"""
Modal deployment of the RF-DETR Large + SAM 2.1 segmentation pipeline.

Pairs with processing.py (your CV logic). Dependencies pinned to your RunPod
requirements.txt, trimmed to inference-only.

Deploy:   modal deploy modal_app.py
Test:     modal run modal_app.py        (needs a local test.jpg)
Call:     POST base64 image -> masks/polygons JSON (proxy auth required)
"""

import modal

app = modal.App("gym-segmentation")

# ---------------------------------------------------------------------------
# 1. Container image — replaces your Dockerfile. torch pinned and installed
#    first so rfdetr/ultralytics resolve against it. Switch python to "3.10"
#    if any dep misbehaves (your proven RunPod env).
# ---------------------------------------------------------------------------
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0")          # OpenCV system libs
    .pip_install("torch==2.5.1", "torchvision==0.20.1")
    .pip_install(
        "rfdetr",
        "ultralytics==8.3.133",
        "supervision==0.25.1",
        "opencv-python-headless==4.10.0.84",
        "scipy==1.13.1",
        "scikit-image==0.24.0",
        "numpy==2.0.2",
        "pillow==11.1.0",
        "fastapi[standard]",          # required for @modal.fastapi_endpoint
    )
    .add_local_python_source("processing")
)

# ---------------------------------------------------------------------------
# 2. Volume holding model weights. Upload once:
#      modal volume create gym-models
#      modal volume put gym-models ./checkpoint_best_ema.pth /checkpoint_best_ema.pth
#      modal volume put gym-models ./sam2.1_b.pt            /sam2.1_b.pt
# ---------------------------------------------------------------------------
model_volume = modal.Volume.from_name("gym-models", create_if_missing=True)
MODEL_DIR = "/models"


@app.cls(
    gpu="L4",                       # low-VRAM, modern, cost-efficient. Try "A10G" for more compute.
    image=image,
    volumes={MODEL_DIR: model_volume},
    scaledown_window=300,           # stay warm 5 min after a request (fewer cold starts between calls)
    min_containers=0,               # scale to zero (set 1 to eliminate cold starts entirely, at continuous cost)
    max_containers=5,               # cost safety cap
    enable_memory_snapshot=True,    # CPU snapshot: skip torch import + file reads on cold boot
    experimental_options={"enable_gpu_snapshot": True},  # GPU snapshot (ALPHA): also restore model GPU state
)
@modal.concurrent(max_inputs=1)     # one image per container; raise once you confirm GPU headroom
class Segmenter:
    @modal.enter(snap=True)
    def load_models(self):
        """Runs ONCE per container start: load weights, optimize, warm up."""
        import torch
        import numpy as np
        from PIL import Image
        from rfdetr import RFDETRLarge
        from ultralytics import SAM

        self.rf_detr = RFDETRLarge(pretrain_weights=f"{MODEL_DIR}/checkpoint_best_ema.pth",
                                   num_classes=65)  # new checkpoint has 65 classes (head=66); was 58
        self.sam = SAM(f"{MODEL_DIR}/sam2.1_b.pt")

        if torch.cuda.is_available():
            torch.backends.cudnn.benchmark = True
            try:
                self.sam.model = self.sam.model.cuda()
            except Exception as e:
                print(f"SAM->GPU note: {e}")

        # Compile/optimize RF-DETR for faster inference (the warning you saw).
        # NOTE: may slightly change outputs — re-verify parity vs RunPod after enabling.
        try:
            self.rf_detr.optimize_for_inference()
            print("RF-DETR optimized for inference")
        except Exception as e:
            print(f"optimize_for_inference skipped: {e}")

        # Warm-up so the first real request doesn't pay kernel-compile / optimize cost.
        if torch.cuda.is_available():
            try:
                dummy = np.zeros((1024, 1024, 3), dtype=np.uint8)
                self.rf_detr.predict(Image.fromarray(dummy), threshold=0.5)
                self.sam.predict(source=dummy, bboxes=np.array([[10, 10, 200, 200]]),
                                 imgsz=1024, half=True, device="cuda:0", verbose=False)
                print("Warm-up complete")
            except Exception as e:
                print(f"Warm-up skipped: {e}")

    @modal.method()
    def predict(self, image_b64: str, confidence_threshold: float = 0.25):
        """Your handler() body, minus the RunPod job wrapper."""
        import base64
        from io import BytesIO
        from PIL import Image
        from processing import process_image_with_masks

        if image_b64.startswith("data:image"):
            image_b64 = image_b64.split(",")[1]
        image = Image.open(BytesIO(base64.b64decode(image_b64))).convert("RGB")
        result = process_image_with_masks(self.rf_detr, self.sam, image, confidence_threshold)
        return {"status": "success", "result": result}


# ---------------------------------------------------------------------------
# 3. HTTP endpoint — proxy auth required (callers send Modal-Key / Modal-Secret).
#    POST {"image": "<base64>", "confidence_threshold": 0.25}
# ---------------------------------------------------------------------------
@app.function(image=image)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def segment(item: dict):
    image_b64 = item["image"]
    conf = item.get("confidence_threshold", 0.25)
    return Segmenter().predict.remote(image_b64, conf)


# ---------------------------------------------------------------------------
# 4. ASYNC submit-then-poll endpoints (RunPod-style /run + /status).
#    Use these for long requests that might exceed the 150s web timeout.
#    This is a lightweight CPU web server; the GPU work runs in Segmenter.
#
#      POST /submit  {"image": "<b64>", "confidence_threshold": 0.25}
#            -> {"call_id": "fc-..."}                  (returns immediately)
#      GET  /result/{call_id}
#            -> 200 + result JSON   (done)
#            -> 202 {"status":"running"}   (still processing)
#            -> 404 {"status":"expired"}   (result no longer available)
# ---------------------------------------------------------------------------
@app.function(image=image)
@modal.concurrent(max_inputs=50)        # web layer handles many light requests at once
@modal.asgi_app(requires_proxy_auth=True)
def web():
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    web_app = FastAPI()

    @web_app.post("/submit")
    def submit(item: dict):
        image_b64 = item["image"]
        conf = item.get("confidence_threshold", 0.25)
        call = Segmenter().predict.spawn(image_b64, conf)   # non-blocking
        return {"call_id": call.object_id}

    @web_app.get("/result/{call_id}")
    def result(call_id: str):
        fc = modal.FunctionCall.from_id(call_id)
        try:
            return fc.get(timeout=0)                          # ready -> result
        except TimeoutError:
            return JSONResponse({"status": "running"}, status_code=202)
        except modal.exception.OutputExpiredError:
            return JSONResponse({"status": "expired"}, status_code=404)

    return web_app


@app.local_entrypoint()
def main():
    import base64
    with open("test.jpg", "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    print(Segmenter().predict.remote(b64))
