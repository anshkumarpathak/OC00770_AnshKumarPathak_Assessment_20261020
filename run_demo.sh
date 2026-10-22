#!/usr/bin/env sh
# One-click launcher for the demo portal on macOS / Linux.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "Creating the Python environment in .venv ..."
    python3 -m venv .venv
fi
echo "Checking packages ..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
echo "Starting the demo portal at http://localhost:8501  (press Ctrl+C to stop)"
exec .venv/bin/python -m streamlit run dashboard/app.py --server.address 127.0.0.1 --server.port 8501
