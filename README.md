# Zory product pipeline

Ingestion and enrichment for the product catalogue, plus the reviewer app.

Django + DRF (API only) with a Next.js frontend in `frontend/`. Local Postgres.

## Quick start

```bash
docker compose up -d db
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # fill in; nothing sensitive has a default
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py seed_products
./.venv/bin/python manage.py createsuperuser
./.venv/bin/python manage.py runserver
```

```bash
./.venv/bin/python -m pytest
```

### Serving: ASGI (Daphne)

The backend is served over ASGI by Daphne. `daphne` sits first in `INSTALLED_APPS`, so
`manage.py runserver` above already serves ASGI, and local dev matches production. To run
it for real:

```bash
./.venv/bin/daphne -b 0.0.0.0 -p 8000 config.asgi:application
```

- **Why ASGI:** live queue updates (`GET /api/events/`, `pipeline/streaming.py`) keep one
  connection open per reviewer tab. Under ASGI that's an idle coroutine, and one Postgres
  `LISTEN` per process serves every tab. It's also the server Channels runs on, so adding
  WebSockets later needs no second switch.
- **Scaling:** one Daphne process is a single event loop. For more capacity, run N
  processes behind a load balancer or reverse proxy. Each process opens its own single
  `LISTEN` connection.
- **Reverse proxy in front:** turn off response buffering for `/api/events/`
  (`X-Accel-Buffering: no` already covers nginx), and keep the read timeout above 20s,
  which is the stream's keep-alive interval.
- `CONN_MAX_AGE` must stay 0 (see `config/settings.py`).
- The SSE stream is async and only works under ASGI. Rolling back to a WSGI server means
  reverting this change, not only swapping the server command.

### Frontend

```bash
cd frontend && npm install && npm run dev     # http://localhost:3000
```

`next.config.mjs` proxies `/api/*` to Django, so the browser sees one origin: session
cookies work with no CORS config, and the CSRF cookie stays readable for the
`X-CSRFToken` header.

Two details that are easy to get wrong there — Next strips the trailing slash from a
rewrite destination, and Django's `APPEND_SLASH` cannot redirect a POST without losing
its body. So the config both disables the client-side slash redirect and re-appends the
slash on the destination. Remove either and every write fails with a 308 or a 500.

## How it works

**The database is the queue.** A stage has no work list of its own — it declares
*who is eligible* as a query, and a worker claims matching rows with
`SELECT … FOR UPDATE SKIP LOCKED`. One consequence worth internalising: it doesn't
matter how a row reached that state. The API, a management command, an import, or a
`psql` session all drive the pipeline identically.

Approving a **category** is the trigger. It makes three stages eligible at once:

```
category_status = APPROVED ──┬──▶ ingestion
                             ├──▶ 2D icon   (ALLOWED_CATEGORIES only)
                             └──▶ metadata  (ALLOWED_CATEGORIES only)

dimensions_status = APPROVED ──▶ gates nothing; read-time only, for layout
```

Approving **dimensions** starts nothing. It's checked at read time by the layout
feature, so correcting a measurement never re-runs a stage.

### Status vocabularies

| Family | Values | Used by |
|---|---|---|
| `ReviewStatus` | PENDING · IN_REVIEW · APPROVED · REJECTED · FAILED · GRANDFATHERED | category, dimensions, icon, 3D |
| `JobStatus` | PENDING · IN_PROGRESS · COMPLETED · FAILED | ingestion |
| *(none)* | — | metadata: completion is derived from the data, bounded by `metadata_attempts` |

`GRANDFATHERED` is unused on an empty database. It exists so that when this points at
the real catalogue, "trusted because it was already live" stays distinguishable from
"a human approved it" — a distinction you can never recover once it's lost.

`detection` is **nullable**: `NULL` means nothing has looked yet, `False` means the
model looked and found nothing. Paired with `ingestion_status`, a failed run is always
distinguishable from a negative result.

## Layout

```
config/          settings, urls
pipeline/
  categories.py  the detector's class vocabulary + normalised lookup
  enums.py       status vocabularies
  models.py      Store, Product, ReviewEvent
  transitions.py the only sanctioned way a status changes
  serializers.py
  views.py       review queues + decision endpoints
  tests/
frontend/        Next.js reviewer app
fixtures/        seed data — see fixtures/README.md
```

## Schema

`Store` and `Product` mirror the live `core_store` / `core_product` tables — same
column names, same types, same constraints — so pointing this at the real database is
`ADD COLUMN`s plus a connection string, not a migration project.

Three deliberate deviations, each commented at the field:

1. `detection` is nullable (source defaults to `False`, which conflates two states)
2. `pinecone_id` is unique (source assigns it via a check-then-act race)
3. the enrichment status columns are new

## Categories

`pipeline/categories.py` mirrors the detection model's classes **verbatim** — it is not
ours to invent. Most are hyphenated; a handful are spaced because the training data was
annotated that way. We don't rewrite them: a hyphen we invent is a class the model has
never heard of.

Normalisation is a **lookup key only**, applied on both sides of every comparison:

```python
resolve("Leg Press Machine") == "leg press machine"     # canonical, stored verbatim
matches(detector_label, product.category)               # separator-insensitive
```

Without that two-sided normalisation, six classes silently never match and their
products fail detection with no error.

## API

| | |
|---|---|
| `GET  /api/review/category/` | queue (`?store=<id>`) |
| `POST /api/review/category/<id>/decide/` | `{decision, category?, note?}` |
| `GET  /api/review/dimensions/` | queue (`?store=<id>`) |
| `POST /api/review/dimensions/<id>/decide/` | `{decision, length?, width?, height?, note?}` |
| `GET  /api/review/counts/` | queue badges |
| `GET  /api/vocabulary/` | categories + stores |

Every decision writes a `ReviewEvent` in the same transaction as the status change, so
the two can never disagree. Corrections record the previous value.

## Status

**Built** — schema · transitions · sheet import · category + dimension review API ·
Next.js reviewer app (import, category, dimensions) · 49 tests.

**Next** — ingestion stage (Modal → segment → Gemini → Pinecone); icon stage (threaded,
2 retries, `ALLOWED_CATEGORIES` only, S3); metadata stage (Qwen, 2 retries); stage
runner + poller + stuck-state sweeper.

## Import — dev/test only

**Production upload lives in the Zory backend**, which owns the merchant-facing screen,
`ProductFile`, and row creation. This exists so the pipeline can be developed against a
local database without running the backend.

It mirrors the backend's two-phase flow deliberately: whatever the backend produces is
what the stages consume, so the contract is worth having executable.

**1. Upload** (`POST /api/import/upload/`) validates the headers and the store, stores
the file as `{store}_{YYYYMMDDHHMMSS}{ext}`, creates `ProductFile(file_status=1)` and
returns **202 with no counts**. It creates no products.

**2. A worker** claims `file_status=1` (the same `SKIP LOCKED` pattern the stages use,
standing in for the backend's S3-event → Lambda hop), moves it to `2`, creates the rows,
then writes `3`/`4` plus the report into `logs`:

```bash
python manage.py import_sheets
```

The split isn't only for parity — each row costs one `gpt-4o-mini` call, so a large
sheet can't be imported inside a request.

### The sheet contract

**Required** — `product_name`, `price_amount`, `price_unit`, `image_url`,
`product_url`, `category`
**Optional** — `length`, `width`, `height`, `dimension_unit`, `main_color`,
`secondary_colors`, `styles`, `product_color`, `salla_product_id`

### Bilingual names

The sheet carries **one** `product_name` column; `name_arabic` is not an input. Which
language it is gets detected by script, and the other side is generated with
`gpt-4o-mini` — exactly as `core/utils/helpers.py` does it:

```python
if is_arabic(product_name):
    name_arabic, name_english = product_name, translate_to_english(product_name)
else:
    name_english, name_arabic = product_name, translate_to_arabic(product_name)
```

A translation failure **returns the original text rather than raising**, and a fallback
API key is tried first. So an OpenAI outage, or no key at all, degrades to untranslated
names — it never fails an import.

### Deliberate differences from the backend

- a duplicate `product_url` really is skipped (there it is logged and then imported anyway)
- a row whose category isn't recognised is **kept and flagged** rather than silently
  dropped, because a reviewer looks at every category regardless
- `pinecone_id` is generated the same way (12 digits) but the column is UNIQUE, so a
  collision raises instead of silently sharing a vector id
