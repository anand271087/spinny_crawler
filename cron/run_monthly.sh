#!/usr/bin/env bash
# Cron entry point. Activates venv, runs monthly crawl, exits with crawl status.
set -euo pipefail

PROJECT_DIR="/opt/spinny_crawler"
cd "$PROJECT_DIR"

# shellcheck disable=SC1091
source .venv/bin/activate

# Parallel workers — 4 is the recommended default (caps memory at ~800MB,
# preserves per-site politeness). Override via CRAWLER_MAX_WORKERS if VM
# has more headroom. See docs/runbook.md §Parallelism.
export CRAWLER_MAX_WORKERS="${CRAWLER_MAX_WORKERS:-4}"

python -m orchestrator.run_monthly --config config/sites.yaml
