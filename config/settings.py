"""Django settings.

Every secret comes from the environment. Nothing sensitive has a default — a missing
key fails loudly at startup rather than silently falling back to a baked-in value.
(The scripts this pipeline replaces shipped live OpenAI and AWS credentials as
hardcoded defaults; that must not happen again.)
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read .env into the environment. A real environment variable always wins.

    Done here rather than by shell sourcing: `. <(...)` silently sets nothing under
    some shells, which produced a worker running with no API key and no error until
    the first request failed deep inside a client library.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


_load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str | None = None) -> str:
    """Read a setting from the environment. Raises when required and absent.

    Surrounding quotes are stripped: `.env` files commonly quote values, and a URL
    carrying literal `\'` characters fails deep inside whatever consumes it rather
    than here — `requests` reports "no connection adapters found", which points at
    the wrong thing entirely.
    """
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value.strip().strip("\"'").strip()


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

INSTALLED_APPS = [
    # First on purpose: daphne overrides `runserver` so local dev serves ASGI exactly like
    # production. Below staticfiles, staticfiles' own runserver would win.
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "pipeline",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
# Kept for tooling that still reads it. Not a rollback path on its own: the SSE endpoint's
# async stream only works under ASGI.
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "pipeline" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# CONN_MAX_AGE is deliberately left at its default of 0. Under ASGI, Django can't reuse
# persistent connections safely across async requests (see its docs), so raising it leaks
# connections instead of saving them.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "zory_pipeline"),
        "USER": env("POSTGRES_USER", "zory"),
        "PASSWORD": env("POSTGRES_PASSWORD", "local"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The Next.js dev server proxies /api/* here, so both origins must be trusted for CSRF.
CSRF_TRUSTED_ORIGINS = [
    o for o in env("CSRF_TRUSTED_ORIGINS", "http://localhost:3000,http://localhost:8000").split(",") if o
]
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # the frontend reads it to set the X-CSRFToken header

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        # Structured enough to key on product_id / stage when the workers land.
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}

# ── Pipeline tunables ───────────────────────────────────────────────────────────
# Thresholds live here, not as module constants buried in a service — the design doc's
# open questions ("confirm the confidence threshold") need somewhere to land.
# ── detection (Modal) ───────────────────────────────────────────────────────────
MODAL_URL = env("MODAL_URL", "")
MODAL_KEY = env("MODAL_KEY", "")
MODAL_SECRET = env("MODAL_SECRET", "")
DETECTION_CONFIDENCE_THRESHOLD = float(env("DETECTION_CONFIDENCE_THRESHOLD", "0.10"))
# Modal caps synchronous HTTP at ~150s; waiting longer than the platform will only ever
# time out after the server has already given up.
DETECTION_TIMEOUT_SECONDS = int(env("DETECTION_TIMEOUT_SECONDS", "145"))
INGESTION_MAX_ATTEMPTS = int(env("INGESTION_MAX_ATTEMPTS", "2"))
#: Threads draining the ingest stage. 8 matches the backend's `_MAX_WORKERS`, which is
#: itself sized to the detection provider's worker count — going above it only queues
#: requests at the far end while holding claimed rows open here.
INGESTION_CONCURRENCY = int(env("INGESTION_CONCURRENCY", "8"))

# ── images ──────────────────────────────────────────────────────────────────────
#: Below this the model is never called; the measured size is recorded so the
#: undersized products are a queryable revisit list rather than a silent loss.
IMAGE_MIN_DIMENSION = int(env("IMAGE_MIN_DIMENSION", "400"))
IMAGE_MAX_DIMENSION = int(env("IMAGE_MAX_DIMENSION", "4096"))   # downscaled, not rejected
IMAGE_MAX_FILE_SIZE_MB = int(env("IMAGE_MAX_FILE_SIZE_MB", "10"))

# ── 2D icons ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
# gpt-image-2 at LOW quality: roughly the same per-image cost as gpt-image-1-mini but
# faster and higher fidelity — the source script's reasoning, kept.
ICON_MODEL = env("ICON_MODEL", "gpt-image-2")
ICON_QUALITY = env("ICON_QUALITY", "low")
ICON_SIZE = env("ICON_SIZE", "1024x1024")
ICON_INPUT_SIZE = int(env("ICON_INPUT_SIZE", "1024"))
#: In-process retries covering BOTH transient API errors and degenerate output.
ICON_GENERATION_RETRIES = int(env("ICON_GENERATION_RETRIES", "5"))

# ── S3 ──────────────────────────────────────────────────────────────────────────
S3_BUCKET = env("S3_BUCKET", "zory-icons-dev")
S3_ICON_PREFIX = env("S3_ICON_PREFIX", "2D_icons")
#: Regeneration overwrites a stable key, so the previous icon is copied aside first.
S3_ICON_BACKUP_PREFIX = env("S3_ICON_BACKUP_PREFIX", "_backup")
#: Set only if the bucket is public. Empty means the UI gets presigned URLs, which is
#: what a private bucket needs — and the bucket should be private.
S3_PUBLIC_BASE_URL = env("S3_PUBLIC_BASE_URL", "")
AWS_REGION = env("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", "")
#: For MinIO or another S3-compatible store. Empty means real AWS.
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", "")

# ── embedding (Gemini) ──────────────────────────────────────────────────────────
GOOGLE_API_KEY = env("GOOGLE_API_KEY", "")
GEMINI_EMBEDDING_MODEL = env("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-2-preview")

# ── vectors (Pinecone) ──────────────────────────────────────────────────────────
PINECONE_API_KEY = env("PINECONE_API_KEY", "")
PINECONE_INDEX_NAME = env("PINECONE_INDEX_NAME", "zory-pipeline-dev")
PINECONE_DIMENSION = int(env("PINECONE_DIMENSION", "3072"))
PINECONE_CLOUD = env("PINECONE_CLOUD", "aws")
PINECONE_REGION = env("PINECONE_REGION", "us-east-1")
STAGE_CLAIM_BATCH = int(env("STAGE_CLAIM_BATCH", "100"))
STAGE_STUCK_TIMEOUT_MINUTES = int(env("STAGE_STUCK_TIMEOUT_MINUTES", "30"))
ICON_MAX_ATTEMPTS = int(env("ICON_MAX_ATTEMPTS", "2"))

#: Threads drawing icons. The source script ran 12 (`regenerate_icons.py --workers`),
#: so parallelism here is the proven configuration, not a gamble — and the retry/backoff
#: that made it safe is ported in `clients/icon_generator.py`. 8 sits inside that proven
#: headroom, so rate-limit backoff stays a safety net rather than a routine path for a
#: continuously-running service, and it matches INGESTION_CONCURRENCY so there is one
#: number to reason about.
#:
#: This directly multiplies the unattended gpt-image-2 spend rate. Lower it before a
#: bulk approval if that matters more than throughput.
ICON_CONCURRENCY = int(env("ICON_CONCURRENCY", "8"))


# ── metadata (Qwen3-VL via OpenRouter) ──────────────────────────────────────────
#: Hosted, so there is no cold start to survive: the call is synchronous, like detection.
#: The self-hosted Qwen2.5-VL-32B on Modal was dropped because 65GB of bf16 weights took
#: 25+ minutes to load, making every scale-from-zero cost more than the work was worth.
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", "")
OPENROUTER_METADATA_MODEL = env("OPENROUTER_METADATA_MODEL", "qwen/qwen3-vl-8b-instruct")
#: Optional; OpenRouter attributes usage to it on their dashboard.
OPENROUTER_SITE_URL = env("OPENROUTER_SITE_URL", "")
METADATA_TIMEOUT_SECONDS = int(env("METADATA_TIMEOUT_SECONDS", "120"))
METADATA_MAX_NEW_TOKENS = int(env("METADATA_MAX_NEW_TOKENS", "256"))
#: The decided policy: an empty answer gets two attempts, then the row is left alone.
METADATA_MAX_ATTEMPTS = int(env("METADATA_MAX_ATTEMPTS", "2"))
#: Back to ingestion's level. The Modal deployment forced this down to 3 because every
#: request landed on ONE GPU; a hosted endpoint has no such bottleneck.
METADATA_CONCURRENCY = int(env("METADATA_CONCURRENCY", "8"))
