#!/usr/bin/env bash
# Inicia a harness v2 em tmux. Pode ficar nesta pasta ou usar HARNESS_DIR.
set -euo pipefail
SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname -- "$SELF")"
APP_DIR="${HARNESS_DIR:-$SCRIPT_DIR}"
if [[ ! -f "$APP_DIR/app.py" && -z "${HARNESS_DIR:-}" ]]; then
    APP_DIR="$HOME/qwen_harness_streamlit"
fi
APP_DIR="$(cd -- "$APP_DIR" && pwd)"
SESSION="${HARNESS_TMUX_SESSION:-qwen-harness}"
PY="$APP_DIR/.venv/bin/python"
export HARNESS_DATA_DIR="${HARNESS_DATA_DIR:-$APP_DIR/data}"
export LLAMA_BASE_URL="${LLAMA_BASE_URL:-http://127.0.0.1:8080/v1}"
export LLAMA_MODEL="${LLAMA_MODEL:-qwen}"
export HARNESS_RUNNER_IMAGE="${HARNESS_RUNNER_IMAGE:-qwen-harness-runner:1}"
export PORT="${PORT:-8501}"

fail() { echo "Erro: $*" >&2; exit 1; }

api_ready() {
    "$PY" -c 'import json,sys,urllib.request
try:
    with urllib.request.urlopen(sys.argv[1].rstrip("/")+"/models", timeout=3) as r:
        models=json.load(r).get("data",[])
    sys.exit(0 if any(m.get("id")==sys.argv[2] for m in models) else 1)
except Exception:
    sys.exit(1)' "$LLAMA_BASE_URL" "$LLAMA_MODEL"
}

port_busy() {
    "$PY" -c 'import socket,sys
try:
    s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=1); s.close()
except OSError:
    sys.exit(1)' "$1"
}

# Este modo é chamado nas janelas; serviços recebem o ambiente explicitamente.
if [[ "${1:-}" == '--service' ]]; then
    cd -- "$APP_DIR"
    case "${2:-}" in
        modelo) exec bash iniciar_modelo.sh ;;
        worker) exec bash iniciar_worker.sh ;;
        interface)
            echo 'Aguardando a API do Qwen (até 10 minutos)…'
            ready=0
            for ((i=0; i<300; i++)); do
                if api_ready; then ready=1; break; fi
                sleep 2
            done
            [[ "$ready" == 1 ]] || fail 'A API não ficou pronta. Confira a janela modelo e execute novamente após corrigir.'
            exec bash iniciar_interface.sh ;;
        *) fail 'Serviço inválido.' ;;
    esac
fi

case "${1:-start}" in start|status|attach) ;; *) fail 'Uso: bash iniciar_tudo.sh [start|status|attach]' ;; esac
[[ "$SESSION" =~ ^[a-zA-Z0-9_-]+$ ]] || fail 'Nome de sessão inválido.'
command -v tmux >/dev/null || fail 'Instale tmux: sudo apt install tmux'

show_status() {
    tmux list-panes -s -t "$SESSION" \
        -F '#{window_name}: encerrado=#{pane_dead} (0=rodando, 1=encerrado) | #{pane_current_command}'
    echo "Acompanhar: tmux attach -t $SESSION"
    echo 'No tmux: Ctrl+B e depois N muda de janela; Ctrl+B e depois D desconecta.'
    echo 'Para reiniciar uma janela encerrada: tmux respawn-window -t SESSAO:NOME'
}

if [[ "${1:-}" == status || "${1:-}" == attach ]]; then
    tmux has-session -t "$SESSION" 2>/dev/null || fail "Sessão $SESSION ainda não existe. Execute sem argumentos."
    if [[ "$1" == status ]]; then show_status; exit 0; fi
    if [[ -n "${TMUX:-}" ]]; then exec tmux switch-client -t "$SESSION"; fi
    exec tmux attach-session -t "$SESSION"
fi

# Serializa dois cliques/execuções simultâneos, sem passar o lock aos serviços.
command -v flock >/dev/null || fail 'flock não encontrado (pacote util-linux).'
exec 9>"$APP_DIR/.iniciar-tmux.lock"
flock -n 9 || fail 'Outro inicializador está em execução.'
tmux_call() { command tmux "$@" 9>&-; }

if tmux_call has-session -t "$SESSION" 2>/dev/null; then
    owner="$(tmux_call show-options -qv -t "$SESSION" @harness_dir)"
    [[ "$owner" == "$APP_DIR" ]] || fail 'Já existe uma sessão desse nome para outro diretório. Use HARNESS_TMUX_SESSION.'
    echo 'Sessão já existe; nenhum serviço foi duplicado.'
    show_status
    exit 0
fi

[[ -x "$PY" ]] || fail 'Falta .venv. Rode python3 -m venv .venv e .venv/bin/pip install -r requirements.txt na pasta da harness.'
"$PY" -c 'import streamlit,langgraph,langchain_openai,pymupdf4llm,trafilatura,requests' \
    || fail 'Dependências ausentes; rode .venv/bin/pip install -r requirements.txt'
for script in iniciar_modelo.sh iniciar_worker.sh iniciar_interface.sh preparar_executor.sh; do
    [[ -f "$APP_DIR/$script" ]] || fail "Arquivo ausente: $script. É necessário o pacote v2 completo."
done

reuse_model=0
if api_ready; then
    reuse_model=1
    echo 'Reutilizando a API do modelo que já está rodando.'
else
    [[ "$LLAMA_BASE_URL" == 'http://127.0.0.1:8080/v1' ]] \
        || fail 'A API personalizada não respondeu. Inicie-a antes ou use o endereço padrão.'
    port_busy 8080 && fail 'Porta 8080 ocupada, mas a API/alias não está pronto. Aguarde ou confira o serviço antes de tentar novamente.'
    [[ "$LLAMA_MODEL" == qwen ]] || fail 'iniciar_modelo.sh cria o alias qwen; use LLAMA_MODEL=qwen.'
    [[ -x "${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}" ]] || fail 'llama-server não encontrado. Configure LLAMA_SERVER.'
    [[ -f "${MODEL_PATH:-$HOME/models/Qwen3-Coder-Next/Qwen3-Coder-Next-Q4_K_M.gguf}" ]] || fail 'Modelo GGUF não encontrado. Configure MODEL_PATH.'
fi
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] || fail 'PORT inválida.'
port_busy "$PORT" && fail "Porta $PORT ocupada. Encerre a interface antiga ou escolha PORT=8502."
command -v docker >/dev/null || fail 'Docker é necessário para o worker da harness v2.'
docker info >/dev/null || fail 'Docker não está disponível para este usuário. Confira docker info.'
if ! docker image inspect "$HARNESS_RUNNER_IMAGE" >/dev/null 2>&1; then
    echo 'Preparando a imagem do executor pela primeira vez…'
    (cd -- "$APP_DIR" && bash preparar_executor.sh) 9>&-
fi

# O tmux pode ter sido iniciado antes destes exports: passar os valores garante
# que todas as janelas compartilhem os mesmos dados e configuração.
SERVICE_ENV=(env "HARNESS_DIR=$APP_DIR" "HARNESS_DATA_DIR=$HARNESS_DATA_DIR"
    "LLAMA_BASE_URL=$LLAMA_BASE_URL" "LLAMA_MODEL=$LLAMA_MODEL"
    "HARNESS_RUNNER_IMAGE=$HARNESS_RUNNER_IMAGE" "PORT=$PORT"
    "THREADS=${THREADS:-20}" "CONTEXT=${CONTEXT:-262144}"
    "MODEL_PATH=${MODEL_PATH:-$HOME/models/Qwen3-Coder-Next/Qwen3-Coder-Next-Q4_K_M.gguf}"
    "LLAMA_SERVER=${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"
    "PATH=$PATH")

reuse_worker=0
if [[ -f "$HARNESS_DATA_DIR/worker.lock" ]] && ! flock -n "$HARNESS_DATA_DIR/worker.lock" true 9>&-; then
    reuse_worker=1
    echo 'Reutilizando o worker que já está ativo neste diretório de dados.'
fi
first=interface
[[ "$reuse_worker" == 1 ]] || first=worker
[[ "$reuse_model" == 1 ]] || first=modelo
tmux_call new-session -d -s "$SESSION" -n "$first" -c "$APP_DIR"
tmux_call set-option -t "$SESSION" @harness_dir "$APP_DIR"

start_window() {
    local name="$1"
    if [[ "$name" != "$first" ]]; then
        tmux_call new-window -d -t "$SESSION:" -n "$name" -c "$APP_DIR"
    fi
    tmux_call set-window-option -t "$SESSION:$name" remain-on-exit on
    tmux_call respawn-window -k -t "$SESSION:$name" -c "$APP_DIR" \
        "${SERVICE_ENV[@]}" bash "$SELF" --service "$name"
}
[[ "$reuse_model" == 1 ]] || start_window modelo
[[ "$reuse_worker" == 1 ]] || start_window worker
start_window interface

ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
echo "Inicialização disparada. Abra http://${ip:-127.0.0.1}:$PORT quando o modelo terminar de carregar."
echo 'Os processos continuam rodando se você fechar o SSH.'
show_status
