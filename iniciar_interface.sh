#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    echo 'Crie o ambiente primeiro: python3 -m venv .venv'
    echo 'Depois: .venv/bin/pip install -r requirements.txt'
    exit 1
fi
exec .venv/bin/python -m streamlit run app.py \
    --server.address 0.0.0.0 --server.port "${PORT:-8501}" "$@"
