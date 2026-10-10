#!/usr/bin/env bash
set -euo pipefail
python3 -m pytest -q
python3 scripts/scan_credentials.py .
python3 scripts/scan_personal.py .
