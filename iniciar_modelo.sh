#!/usr/bin/env bash
set -euo pipefail
BIN="${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"
MODEL="${MODEL_PATH:-$HOME/models/Qwen3-Coder-Next/Qwen3-Coder-Next-Q4_K_M.gguf}"
exec "$BIN" -m "$MODEL" --alias qwen --jinja -ngl 0 \
    -t "${THREADS:-20}" -c "${CONTEXT:-262144}" \
    --host 127.0.0.1 --port 8080 "$@"
