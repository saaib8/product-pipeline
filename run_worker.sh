#!/usr/bin/env bash
# Dev entry point for the pipeline worker.
#
# The loop used to live here, wrapping the one-shot `run_stage`. It now lives inside
# `manage.py run_worker`, which does the same drain but waits on a Postgres LISTEN
# instead of a fixed sleep — so an approval starts work in milliseconds rather than on
# the next tick, while the interval below remains the safety net that makes a lost
# notification a latency problem and never a stranded product.
#
# Stop with Ctrl-C or `pkill -f run_worker`. Anything approved while it is down waits
# in the database and is claimed on the next start.
set -uo pipefail
cd "$(dirname "$0")"

exec ./.venv/bin/python -u manage.py run_worker --interval "${WORKER_INTERVAL:-20}" "$@"
