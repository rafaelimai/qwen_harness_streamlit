#!/usr/bin/env bash
# Encerra os serviços gerenciados pela sessão tmux da harness.
set -euo pipefail

SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname -- "$SELF")"
APP_DIR="${HARNESS_DIR:-$SCRIPT_DIR}"
if [[ ! -f "$APP_DIR/app.py" && -z "${HARNESS_DIR:-}" ]]; then
    APP_DIR="$HOME/qwen_harness_streamlit"
fi
APP_DIR="$(cd -- "$APP_DIR" && pwd)"
SESSION="${HARNESS_TMUX_SESSION:-qwen-harness}"

fail() { echo "Erro: $*" >&2; exit 1; }

command -v tmux >/dev/null || fail 'tmux não está instalado.'
[[ "$SESSION" =~ ^[a-zA-Z0-9_-]+$ ]] || fail 'Nome de sessão inválido.'

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "A harness já está encerrada: a sessão $SESSION não existe."
    exit 0
fi

owner="$(tmux show-options -qv -t "$SESSION" @harness_dir)"
[[ -n "$owner" ]] || fail "A sessão $SESSION não possui identificação da harness; nada foi encerrado."
[[ "$owner" == "$APP_DIR" ]] \
    || fail "A sessão $SESSION pertence a outro diretório ($owner); nada foi encerrado."

tmux kill-session -t "$SESSION"
echo "Harness encerrada: sessão $SESSION finalizada."
