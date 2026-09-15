"""Constrain ONLY main_color. Compare against the 82%/13% control baseline."""
import base64, io, os, threading, time, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); django.setup()
import requests
from django.conf import settings
from pipeline.metadata_vocab import ALL_COLORS
from pipeline.models import Product
from pipeline.services.image_io import ImageIOService
from pipeline.services.metadata_prompt import build_prompt, parse_metadata

# main_color is the ONLY constrained field. styles / secondary stay free-form so the
# schema cannot distort them; `_canon` validates those exactly as before.
NARROW = {"type": "object", "additionalProperties": False,
          "required": ["styles", "main_color", "secondary_colors"],
          "properties": {
              "styles": {"type": "array", "items": {"type": "string"}},
              "main_color": {"type": "string", "enum": list(ALL_COLORS)},
              "secondary_colors": {"type": "array", "items": {"type": "string"}}}}

io_svc = ImageIOService(min_dimension=1, max_dimension=settings.IMAGE_MAX_DIMENSION,
                        max_file_size_mb=settings.IMAGE_MAX_FILE_SIZE_MB)
rows = list(Product.objects.filter(metadata_status="COMPLETED").order_by("pk")
            .values("id", "category", "image_url")[:40])
rows.append(Product.objects.filter(pk=76).values("id","category","image_url").first())

def ask(b64, cat, schema):
    body = {"model": settings.OPENROUTER_METADATA_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": build_prompt(cat)},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}],
            "max_tokens": 256, "temperature": 0}
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "m", "strict": True, "schema": schema}}
    b = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                               "Content-Type": "application/json"}, json=body, timeout=120).json()
    return parse_metadata(b["choices"][0]["message"]["content"]) if b.get("choices") else None

out, lock = {}, threading.Lock()
def work(row):
    try:
        img = io_svc.read_from_url(row["image_url"])
        buf = io.BytesIO(); img.convert("RGB").save(buf, format="JPEG", quality=95)
        b64 = base64.b64encode(buf.getvalue()).decode()
        with lock: out[row["id"]] = (ask(b64, row["category"], None),
                                     ask(b64, row["category"], NARROW))
    except Exception:
        with lock: out[row["id"]] = (None, None)

th = [threading.Thread(target=work, args=(r,)) for r in rows]
for i in range(0, len(th), 8):
    for t in th[i:i+8]: t.start()
    for t in th[i:i+8]: t.join()

ok = [(a, b) for a, b in out.values() if a and b]
n = len(ok)
print(f"NARROW SCHEMA (main_color only), n={n}\n")
print(f"  styles identical          : {sum(1 for a,b in ok if a['styles']==b['styles'])}/{n}"
      f"   (control baseline 82%)")
print(f"  secondary empty (no schema): {sum(1 for a,_ in ok if not a['secondary_colors'])}/{n}")
print(f"  secondary empty (narrow)   : {sum(1 for _,b in ok if not b['secondary_colors'])}/{n}"
      f"   (full schema was 28%)")
print(f"  blank main_color (no schema): {sum(1 for a,_ in ok if not a['main_color'])}/{n}")
print(f"  blank main_color (narrow)   : {sum(1 for _,b in ok if not b['main_color'])}/{n}")
p76 = out.get(76)
if p76 and p76[1]:
    print(f"\n  #76 (Silver): no schema -> {p76[0]['main_color']!r} | narrow -> {p76[1]['main_color']!r}")
