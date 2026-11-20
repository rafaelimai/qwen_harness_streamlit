"""Interface pessoal Streamlit: chat, documentos, worker, artefatos e conversas nomeadas."""
import io
import json
import mimetypes
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

import psutil
import requests
import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict
from agente import build_agent, complete_pending_calls
from ingestao import build_ingestion
from jobs import JobStore, TERMINAL, data_dir
from storage import (artifacts, create_session, safe_name, save_messages,
                     session_meta, rename_session)

st.set_page_config(page_title='Qwen · Homelab', page_icon='🧪', layout='wide')
BASE = data_dir()
BASE.mkdir(parents=True, exist_ok=True)

if 'root' not in st.session_state:
    st.session_state.root = str(create_session(BASE))
    st.session_state.messages = []
root = Path(st.session_state.root)
(root / 'work').mkdir(exist_ok=True)
meta = session_meta(root)
store = JobStore(BASE)

with st.sidebar:
    st.title('🧪 Qwen · Homelab')
    if st.button('Nova conversa', use_container_width=True):
        st.session_state.root = str(create_session(BASE))
        st.session_state.messages = []
        st.session_state.pop('pending_prompt', None)
        st.rerun()
    sessions = sorted((p for p in BASE.iterdir() if p.is_dir() and (p / 'inputs').is_dir()),
                      key=lambda p: (p / 'conversation.json').stat().st_mtime if (p / 'conversation.json').exists() else p.stat().st_mtime,
                      reverse=True)
    labels = {p.name: f"{session_meta(p)['title']} · {session_meta(p)['created_at'][:10]}" for p in sessions}
    # Nomes repetidos continuam distinguíveis, sem exibir o identificador longo.
    for identifier, label in list(labels.items()):
        if list(labels.values()).count(label) > 1:
            for other in list(labels):
                if labels[other] == label:
                    labels[other] += ' · ' + other[-4:]
    selected = st.selectbox('Conversas salvas', [p.name for p in sessions],
                            format_func=lambda sid: labels[sid],
                            index=next((i for i,p in enumerate(sessions) if p == root), 0))
    if st.button('Abrir conversa'):
        chosen = BASE / selected
        history = chosen / 'conversation.json'
        try:
            messages = messages_from_dict(json.loads(history.read_text(encoding='utf-8'))) if history.exists() else []
            messages.extend(complete_pending_calls(messages))
            st.session_state.root = str(chosen)
            st.session_state.messages = messages
            st.session_state.pop('pending_prompt', None)
            st.rerun()
        except (ValueError, OSError) as exc:
            st.error(f'Não consegui abrir: {exc}')
    with st.form('rename_' + root.name):
        new_title = st.text_input('Nome desta conversa', meta['title'], max_chars=100)
        if st.form_submit_button('Salvar nome'):
            try:
                rename_session(root, new_title)
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with st.expander('Conexão e geração'):
        base_url = st.text_input('API do llama.cpp', os.environ.get('LLAMA_BASE_URL', 'http://127.0.0.1:8080/v1'))
        model = st.text_input('Modelo / alias', os.environ.get('LLAMA_MODEL', 'qwen'))
        max_tokens = st.number_input('Máximo de tokens por resposta', min_value=128, max_value=16384, value=4096, step=128)
        temperature = st.slider('Temperatura', 0.0, 1.0, 0.2, 0.1)
        if st.button('Testar conexão'):
            try:
                response = requests.get(base_url.rstrip('/') + '/models', timeout=10)
                response.raise_for_status()
                available = [m['id'] for m in response.json().get('data', [])]
                st.success('API conectada: ' + ', '.join(available))
            except Exception as exc:
                st.error(f'Não foi possível conectar: {exc}')
        st.caption('Contexto e threads são configurados no llama-server.')

st.title(meta['title'] if meta['title'] != 'Nova conversa' else 'Seu laboratório, em conversa')
st.caption('Qwen local • ferramentas LangGraph • execução Python em tarefas isoladas')
monitor_tab, chat_tab, docs_tab, jobs_tab, files_tab = st.tabs(
    ['📊 Sistema', '💬 Conversa', '📄 Documentos', '⚙️ Tarefas', '📦 Arquivos'])

with docs_tab:
    st.subheader('Adicionar arquivos')
    st.caption('Conversões diretas não dependem do Qwen. PDFs sem texto exigem OCR adicional.')
    uploads = st.file_uploader('PDFs, dados ou scripts',
        type=['pdf','csv','json','txt','md','py','sql','yaml','yml','toml'], accept_multiple_files=True)
    if st.button('Salvar arquivos enviados', disabled=not uploads):
        for upload in uploads:
            path = root / 'inputs' / f'{uuid4().hex[:8]}_{safe_name(upload.name)}'
            path.write_bytes(upload.getvalue())
        st.success('Arquivos salvos em inputs/. Scripts executáveis precisam ser gravados em work/ pelo agente.')
    pdfs = sorted((root / 'inputs').glob('*.pdf'))
    selected_pdf = st.selectbox('PDF salvo nesta conversa', [str(p.relative_to(root)) for p in pdfs], index=0 if pdfs else None)
    if st.button('Converter PDF para Markdown', disabled=not pdfs):
        try:
            with st.spinner('Convertendo PDF…'):
                result = build_ingestion(root).invoke({'source': selected_pdf, 'kind': 'pdf'})
            st.success('Gerado: ' + result['markdown_path'])
            for warning in result['warnings']:
                st.warning(warning)
        except Exception as exc:
            st.error(f'Conversão falhou: {exc}')
    url = st.text_input('URL de uma página HTML', placeholder='https://…')
    if st.button('Converter página para Markdown', disabled=not url.strip()):
        try:
            with st.spinner('Baixando e convertendo página…'):
                result = build_ingestion(root).invoke({'source': url.strip(), 'kind': 'web'})
            st.success('Gerado: ' + result['markdown_path'])
        except Exception as exc:
            st.error(f'Conversão falhou: {exc}')


@st.fragment(run_every='3s')
def task_panel(root_path: str):
    """Renderiza o estado da fila e os controles das tarefas da conversa ativa."""
    active_root = Path(root_path)
    queue = JobStore(active_root.parent)
    st.subheader('Execuções Python')
    st.caption(queue.worker_status())
    st.caption('2 CPUs · 2 GiB RAM · sem rede · 256 MiB de trabalho temporário · uma tarefa por vez')
    jobs = queue.list(active_root)
    if not jobs:
        st.info('Peça ao agente para gravar um script em work/ e executá-lo.')
    for job in jobs:
        title = f"{job['request']['script']} — {job['status']} — {job['job_id'][-8:]}"
        with st.expander(title):
            st.json({k: job[k] for k in ('job_id','status','exit_code','error','created_at','started_at','finished_at','request','artifacts')})
            for stream in ('stdout','stderr'):
                if stream + '_tail' in job:
                    st.caption(stream)
                    st.code(job[stream + '_tail'])
                    path = active_root / job[stream + '_path']
                    st.download_button('Baixar ' + stream, path.read_bytes(),
                                       file_name=job['job_id'] + '-' + stream + '.log',
                                       key=job['job_id'] + stream)
            if job['status'] not in TERMINAL:
                if st.button('Cancelar tarefa', key='cancel_' + job['job_id']):
                    queue.cancel(active_root, job['job_id'])
                    st.rerun()
            elif st.button('Analisar resultado', key='analyze_' + job['job_id']):
                st.session_state.pending_prompt = (
                    f"Consulte get_job_status para a tarefa {job['job_id']}. "
                    'Analise os logs e os artefatos. Explique o resultado ou a causa do erro. '
                    'Não enfileire nova execução sem eu pedir.')
                st.rerun()


def system_temperature():
    """Retorna a maior temperatura exposta pelo kernel, quando disponível."""
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    readings = [reading.current for group in sensors.values() for reading in group
                if reading.current is not None]
    return max(readings) if readings else None


def llama_process():
    """Localiza o llama-server iniciado no host, se estiver em execução."""
    expected = Path(os.environ.get('LLAMA_SERVER', 'llama-server')).name
    for process in psutil.process_iter(['name', 'cmdline', 'memory_info', 'create_time']):
        try:
            command = process.info['cmdline'] or []
            executable = Path(command[0]).name if command else process.info['name']
            if executable == expected or executable == 'llama-server':
                return process
        except (psutil.AccessDenied, psutil.NoSuchProcess, IndexError):
            continue
    return None


def model_log_tail():
    """Captura as linhas recentes da janela do modelo quando gerenciada pelo tmux."""
    session = os.environ.get('HARNESS_TMUX_SESSION', 'qwen-harness')
    if not session.replace('-', '').replace('_', '').isalnum():
        return ''
    try:
        result = subprocess.run(
            ['tmux', 'capture-pane', '-p', '-t', f'{session}:modelo', '-S', '-60'],
            capture_output=True, text=True, timeout=1, check=False)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def render_model_status(base_url: str, model: str, sampled_at: float):
    """Mostra prontidão da API e consumo do processo durante o carregamento."""
    ready = False
    available = []
    try:
        response = requests.get(base_url.rstrip('/') + '/models', timeout=1)
        response.raise_for_status()
        available = [item.get('id', '') for item in response.json().get('data', [])]
        ready = model in available
    except (requests.RequestException, ValueError, TypeError):
        pass

    process = llama_process()
    if ready:
        st.success(f'Modelo **{model}** pronto para uso.')
    elif process:
        st.warning(f'Carregando o modelo **{model}**; a API ainda não está pronta.')
    else:
        st.error(f'API indisponível e processo do modelo não encontrado em `{base_url}`.')

    if process:
        try:
            info = process.as_dict(attrs=['pid', 'memory_info', 'create_time', 'cpu_times'])
            cpu_total = info['cpu_times'].user + info['cpu_times'].system
            previous = st.session_state.get('model_process_sample')
            cpu_rate = 0.0
            if previous and previous['pid'] == info['pid']:
                elapsed = max(sampled_at - previous['sampled_at'], 0.001)
                cpu_rate = max(cpu_total - previous['cpu_total'], 0) / elapsed * 100
            st.session_state.model_process_sample = {
                'pid': info['pid'], 'sampled_at': sampled_at, 'cpu_total': cpu_total}
            uptime = max(time.time() - info['create_time'], 0)
            pid_col, cpu_col, memory_col, uptime_col = st.columns(4)
            pid_col.metric('PID do modelo', str(info['pid']))
            cpu_col.metric('CPU do modelo', f'{cpu_rate:.1f}%')
            memory_col.metric('RAM do modelo', f"{info['memory_info'].rss / 1024**3:.1f} GiB")
            uptime_col.metric('Tempo em execução', f'{uptime / 60:.1f} min')
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    elif available:
        st.caption('A API respondeu com: ' + ', '.join(available))

    logs = model_log_tail()
    if logs:
        with st.expander('Carregamento do llama-server', expanded=not ready):
            st.code(logs, language='text')


@st.fragment(run_every='2s')
def system_monitor(base_url: str, model: str):
    """Exibe uso dos recursos do host sem recarregar o restante da interface."""
    sampled_at = time.monotonic()
    render_model_status(base_url, model, sampled_at)
    st.divider()
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    disk_io = psutil.disk_io_counters()
    temperature = system_temperature()

    read_rate = write_rate = 0.0
    previous = st.session_state.get('system_monitor_sample')
    if previous and disk_io:
        elapsed = max(sampled_at - previous['sampled_at'], 0.001)
        read_rate = max(disk_io.read_bytes - previous['read_bytes'], 0) / elapsed
        write_rate = max(disk_io.write_bytes - previous['write_bytes'], 0) / elapsed
    if disk_io:
        st.session_state.system_monitor_sample = {
            'sampled_at': sampled_at,
            'read_bytes': disk_io.read_bytes,
            'write_bytes': disk_io.write_bytes,
        }

    cpu_col, ram_col, disk_col, temp_col = st.columns(4)
    cpu_col.metric('CPU', f'{cpu:.1f}%')
    ram_col.metric('RAM', f'{ram.percent:.1f}%',
                   f'{ram.available / 1024**3:.1f} GiB livres', delta_color='off')
    disk_col.metric('Disco /', f'{disk.percent:.1f}%',
                    f'{disk.free / 1024**3:.1f} GiB livres', delta_color='off')
    temp_col.metric('Temperatura',
                    f'{temperature:.0f} °C' if temperature is not None else 'Indisponível')

    read_col, write_col, load_col = st.columns(3)
    read_col.metric('Leitura do disco', f'{read_rate / 1024**2:.1f} MiB/s')
    write_col.metric('Gravação no disco', f'{write_rate / 1024**2:.1f} MiB/s')
    try:
        load_col.metric('Load average', f'{psutil.getloadavg()[0]:.2f}', '1 minuto')
    except (AttributeError, OSError):
        load_col.metric('Load average', 'Indisponível')


with jobs_tab:
    task_panel(str(root))

with monitor_tab:
    st.subheader('Recursos do servidor')
    st.caption('Estado do modelo e métricas do host atualizados automaticamente a cada 2 segundos.')
    system_monitor(base_url.strip(), model.strip())

with chat_tab:
    st.caption('Exemplo: “Crie um script Python, salve em work/exemplo.py e execute.”')
    for message in st.session_state.messages:
        if isinstance(message, HumanMessage):
            with st.chat_message('user'):
                st.markdown(message.content)
        elif isinstance(message, AIMessage):
            if message.content:
                with st.chat_message('assistant'):
                    st.markdown(message.content)
            for call in message.tool_calls:
                with st.expander('🔧 ' + call['name']):
                    st.json(call['args'])
        elif isinstance(message, ToolMessage):
            with st.expander('Resultado da ferramenta'):
                st.code(str(message.content), language='json')
    # Opções de ask_user podem ser respondidas com um clique ou por texto livre.
    for previous in reversed(st.session_state.messages):
        if isinstance(previous, HumanMessage):
            break
        if isinstance(previous, ToolMessage) and previous.name == 'ask_user':
            try:
                question = json.loads(previous.content)
                for i, option in enumerate(question.get('options', [])):
                    if st.button(option, key=f'answer_{previous.tool_call_id}_{i}'):
                        st.session_state.pending_prompt = option
                        st.rerun()
            except (ValueError, TypeError):
                pass
            break
    prompt = st.chat_input('O que vamos investigar?')
    pending = st.session_state.pop('pending_prompt', None)
    prompt = prompt or pending
    if prompt:
        if meta['title'] == 'Nova conversa':
            rename_session(root, ' '.join(prompt.split())[:72])
        st.session_state.messages.append(HumanMessage(content=prompt))
        save_messages(root, st.session_state.messages)
        with st.chat_message('user'):
            st.markdown(prompt)
        try:
            graph = build_agent(root, base_url.strip(), model.strip(), temperature, int(max_tokens))
            with st.status('Consultando Qwen…', expanded=True) as status:
                for update in graph.stream({'messages': st.session_state.messages},
                                           config={'recursion_limit': 24}, stream_mode='updates'):
                    for node, delta in update.items():
                        new_messages = delta.get('messages', [])
                        st.session_state.messages.extend(new_messages)
                        save_messages(root, st.session_state.messages)
                        for message in new_messages:
                            if isinstance(message, AIMessage) and message.tool_calls:
                                st.write('Executando ferramenta: ' + ', '.join(c['name'] for c in message.tool_calls))
                            elif isinstance(message, ToolMessage):
                                st.write('Resultado da ferramenta recebido.')
                status.update(label='Turno concluído', state='complete', expanded=False)
        except Exception as exc:
            st.session_state.messages.extend(complete_pending_calls(st.session_state.messages))
            st.session_state.messages.append(AIMessage(content=f'Não foi possível concluir: {exc}'))
            save_messages(root, st.session_state.messages)
        st.rerun()

with files_tab:
    st.subheader('Arquivos para baixar')
    files = artifacts(root)
    if not files:
        st.info('Scripts de work/ e resultados de outputs/ aparecem aqui.')
    else:
        names = [str(p.relative_to(root)) for p in files]
        selected_file = st.selectbox('Arquivo', names)
        file = root / selected_file
        size = file.stat().st_size
        st.caption(f'{selected_file} · {size / 1024:.1f} KiB')
        if size <= 128 * 1024 * 1024:
            if file.suffix.lower() in ('.md','.json','.txt','.py','.sql','.csv','.yaml','.yml','.toml','.log'):
                with file.open('r', encoding='utf-8', errors='replace') as source:
                    text = source.read(15000)
                languages = {'.py':'python','.sql':'sql','.json':'json','.md':'markdown','.csv':'text'}
                st.code(text, language=languages.get(file.suffix.lower(), 'text'))
                if size > 15000:
                    st.caption('Prévia limitada; o download contém o arquivo completo.')
            st.download_button('Baixar arquivo', file.read_bytes(), file_name=file.name,
                               mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        else:
            st.warning('Arquivo acima de 128 MiB; copie com scp.')
        if st.button('Preparar ZIP com todos os artefatos'):
            if sum(p.stat().st_size for p in files) > 128*1024*1024:
                st.error('Artefatos acima de 128 MiB; baixe individualmente ou use scp.')
            else:
                buffer = io.BytesIO()
                with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
                    for path in files:
                        archive.write(path, str(path.relative_to(root)))
                st.session_state['archive_' + root.name] = buffer.getvalue()
        archive = st.session_state.get('archive_' + root.name)
        if archive:
            st.download_button('Baixar ZIP preparado', archive,
                               file_name=safe_name(session_meta(root)['title']) + '.zip', mime='application/zip')
            st.caption('Clique novamente em Preparar ZIP após gerar novos arquivos.')
    st.download_button('Baixar histórico da conversa',
                       data=(root / 'conversation.json').read_bytes() if (root / 'conversation.json').exists() else b'[]',
                       file_name=safe_name(session_meta(root)['title']) + '-historico.json', mime='application/json')
