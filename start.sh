#!/bin/bash
# OfferDesk one-command start. Creates the venv on first run, then launches the app.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: creating virtual environment..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
echo "Starting OfferDesk..."
exec .venv/bin/python app.py
