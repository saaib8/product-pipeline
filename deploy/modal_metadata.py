"""Qwen2.5-VL-32B metadata endpoint on Modal.

    modal deploy deploy/modal_metadata.py

Serves the submit + poll contract `pipeline/clients/metadata.py` expects:

    POST /            {"image": "<b64>", "prompt": "...", "max_new_tokens": 256}
      -> 202          {"call_id": "..."}
    GET  /result?call_id=...
      -> 202          {"status": "pending"}
      -> 200          {"status": "success", "result": {"text": "..."}}
      -> 200          {"status": "error",   "error": "..."}

**Why submit + poll rather than a plain sync endpoint like detection uses.** bf16 weights
are ~65 GB. A cold container spends minutes loading them into VRAM, while Modal caps
synchronous HTTP at roughly 150 seconds — so a sync endpoint would not be slow on a cold
start, it would fail, and fail again on the retry, and mark the product FAILED at the
moment the model finally became ready. Spawning decouples the two: submit returns
immediately and the caller waits as long as it likes.

**One ASGI app, not two web endpoints.** Each Modal web endpoint gets its own URL;
mounting FastAPI once means `/` and `/result` share a base, which is what lets the client
hold a single `MODAL_METADATA_URL`.

**The prompt is NOT defined here.** It is sent with every request, from
`pipeline/services/metadata_prompt.py`, so the vocabulary the model is shown and the
vocabulary its answer is validated against cannot drift apart across a redeploy.
"""

from __future__ import annotations

import modal

MODEL_ID = "Qwen/Qwen2.5-VL-32B-Instruct"
MAX_PIXELS = 1024 * 28 * 28          # caps Qwen's image tokens, as the notebook does
MAX_IMAGE_SIDE = 1024

app = modal.App("zory-metadata")

#: Weights live in a Volume rather than the image: 65 GB rebuilt on every image change
#: is intolerable, and a Volume is written once and mounted read-only thereafter.
weights = modal.Volume.from_name("qwen-vl-weights", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch==2.5.1",
        "transformers>=4.49.0",
        "accelerate>=1.0.0",
        "qwen-vl-utils",
        "pillow",
        "fastapi[standard]",
        "huggingface_hub[hf_transfer]",
    )
    # hf_transfer turns a ~65 GB pull from "most of an hour" into minutes.
    .env({"HF_XET_HIGH_PERFORMANCE": "1", "HF_HOME": "/weights/hf"})
)


@app.function(image=image, volumes={"/weights": weights}, timeout=60 * 60)
def fetch_weights() -> str:
    """Populate the Volume. Run once: `modal run deploy/modal_metadata.py::fetch_weights`.

    Kept separate from the class so a cold start never races a 65 GB download — by the
    time anything serves traffic the weights are already on the Volume.
    """
    from huggingface_hub import snapshot_download

    path = snapshot_download(MODEL_ID, cache_dir="/weights/hf")
    weights.commit()
    return path


@app.cls(
    image=image,
    gpu="A100-80GB",              # 32B in bf16 is ~64 GB of weights alone
    volumes={"/weights": weights},
    # `device_map="auto"` stages every tensor in HOST memory before dispatching it to
    # the GPU, so the container needs headroom for the whole model on the CPU side too.
    # Without this the first deploy crawled at ~11 MB/s and projected TWO HOURS to load,
    # degrading as it went — the signature of thrashing, not of slow storage.
    memory=96 * 1024,
    cpu=8.0,
    # Snapshot the container AFTER the weights are resident, so later cold starts
    # restore a loaded model in seconds instead of reading 65 GB again. The slow load
    # is then paid once, at deploy time, rather than on every scale-from-zero.
    enable_memory_snapshot=True,
    # GPU snapshot is what actually restores a loaded model; without it the CPU
    # snapshot only skips imports. Alpha, and the same flag `gym-segmentation`
    # already runs in production on this account.
    experimental_options={"enable_gpu_snapshot": True},
    # Long enough that a burst of approvals reuses one boot instead of paying it again.
    scaledown_window=300,
    # A cold start loads 65 GB; the default would kill the container mid-load.
    startup_timeout=20 * 60,
    timeout=10 * 60,
    max_containers=1,             # one GPU; more would multiply cost, not throughput
)
# One image per container to start, matching `gym-segmentation`. 64GB of weights on
# an 80GB card leaves little room for concurrent KV cache; raise only after confirming
# headroom.
@modal.concurrent(max_inputs=1)
class Qwen:
    @modal.enter(snap=True)          # snap=True: the LOAD is what gets snapshotted.
    def load(self):                  # A plain @modal.enter() runs after restore, so the
                                     # 65GB read happened on every single cold start.
        import torch
        from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

        self.torch = torch
        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL_ID,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            attn_implementation="sdpa",
            cache_dir="/weights/hf",
        )
        self.model.eval()
        self.processor = AutoProcessor.from_pretrained(
            MODEL_ID, max_pixels=MAX_PIXELS, cache_dir="/weights/hf"
        )

        # Warm-up, as gym-segmentation does: the first real request should not also pay
        # for kernel compilation and lazy CUDA init.
        try:
            from PIL import Image as _Image
            self._generate(_Image.new("RGB", (64, 64), "white"), "Say OK.", 4)
            print("warm-up complete")
        except Exception as exc:                      # never block startup on this
            print(f"warm-up skipped: {exc}")

    @modal.method()
    def generate(self, image_b64: str, prompt: str, max_new_tokens: int = 256) -> str:
        """Returns the model's RAW text. Parsing and palette validation stay client-side."""
        import base64
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(base64.b64decode(image_b64))).convert("RGB")
        if max(img.size) > MAX_IMAGE_SIDE:
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
        return self._generate(img, prompt, max_new_tokens)

    def _generate(self, img, prompt: str, max_new_tokens: int) -> str:
        """The inference itself. Plain method, so the warm-up can call it before any
        Modal machinery is involved."""
        from qwen_vl_utils import process_vision_info

        messages = [{"role": "user",
                     "content": [{"type": "image", "image": img},
                                 {"type": "text", "text": prompt}]}]
        text = self.processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
        image_inputs, _ = process_vision_info(messages)
        inputs = self.processor(text=[text], images=image_inputs,
                                padding=True, return_tensors="pt").to(self.model.device)

        with self.torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens,
                                      do_sample=False)   # deterministic, as the notebook
        trimmed = out[:, inputs.input_ids.shape[1]:]
        return self.processor.batch_decode(trimmed, skip_special_tokens=True)[0]


@app.function(image=image, timeout=60)
# The web layer is CPU-only and does nothing but spawn and poll, so it must NOT inherit
# the GPU class's concurrency of 1 — otherwise every caller queues behind one poll.
@modal.concurrent(max_inputs=50)
@modal.asgi_app(requires_proxy_auth=True)     # enforces Modal-Key / Modal-Secret
def web():
    from fastapi import FastAPI, Query
    from fastapi.responses import JSONResponse

    api = FastAPI()

    @api.post("/")
    async def submit(item: dict):
        if not item.get("image"):
            return JSONResponse({"status": "error", "error": "image is required"},
                                status_code=400)
        call = Qwen().generate.spawn(
            item["image"], item.get("prompt", ""), int(item.get("max_new_tokens", 256))
        )
        return JSONResponse({"call_id": call.object_id}, status_code=202)

    @api.get("/result")
    async def result(call_id: str = Query(...)):
        try:
            fc = modal.FunctionCall.from_id(call_id)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": f"unknown call: {exc}"},
                                status_code=404)
        try:
            # `timeout=0` polls without waiting: still-running is the expected answer
            # for the first minutes of a cold start. The `.aio` variant matters — the
            # blocking call stalls the FastAPI event loop for ~220ms per poll, which
            # with several products in flight serialises every caller behind each other.
            text = await fc.get.aio(timeout=0)
        except TimeoutError:
            return JSONResponse({"status": "pending"}, status_code=202)
        except modal.exception.OutputExpiredError:
            # Modal drops results after a retention window. Distinct from a failure:
            # the work happened, the answer is simply no longer retrievable.
            return JSONResponse({"status": "error", "error": "result expired"},
                                status_code=404)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)[:400]},
                                status_code=200)
        return {"status": "success", "result": {"text": text}}

    return api
