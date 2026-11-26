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
import streamlit.components.v1 as components
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict
from agente import _clean_option, build_agent, complete_pending_calls
from ingestao import build_ingestion
from jobs import JobStore, TERMINAL, data_dir
from security import SecurityStore, assess_file, format_alerts, safe_to_display
from storage import (artifacts, create_session, safe_name, save_messages,
                     decision_log, question_cycles, record_decision,
                     save_session_settings, session_meta, session_settings,
                     rename_session)

st.set_page_config(page_title='Qwen · Homelab', page_icon='🧪', layout='wide')
BASE = data_dir()
BASE.mkdir(parents=True, exist_ok=True)
UPLOAD_TYPES = ['pdf','csv','json','txt','md','py','sql','yaml','yml','toml']
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILES = 10


def scroll_to_latest():
    """Solicita ao navegador rolagem suave até o conteúdo mais recente da página."""
    components.html('''
<script>
const scrollLatest = () => {
  const doc = window.parent.document;
  const frame = window.frameElement;

  // O iframe deste componente é o marcador mais confiável do fim da conversa.
  if (frame) frame.scrollIntoView({behavior: 'smooth', block: 'end'});

  // Streamlit mudou o elemento rolável entre versões; cubra os seletores conhecidos.
  const candidates = [
    doc.querySelector('[data-testid="stMain"]'),
    doc.querySelector('section.main'),
    doc.querySelector('[data-testid="stAppViewContainer"]')
  ].filter(Boolean);

  // Inclua qualquer ancestral realmente rolável do marcador.
  let node = frame && frame.parentElement;
  while (node) {
    const style = window.parent.getComputedStyle(node);
    if (node.scrollHeight > node.clientHeight && /(auto|scroll)/.test(style.overflowY)) {
      candidates.push(node);
    }
    node = node.parentElement;
  }

  [...new Set(candidates)].forEach(element => {
    element.scrollTo({top: element.scrollHeight, behavior: 'smooth'});
  });
  window.parent.scrollTo({top: doc.documentElement.scrollHeight, behavior: 'smooth'});
};

// A primeira passagem cobre o rerun; as seguintes cobrem fontes, cartões e expansores
// que alteram a altura logo depois que o componente entra no DOM.
requestAnimationFrame(scrollLatest);
[100, 300, 700].forEach(delay => setTimeout(scrollLatest, delay));
</script>
''', height=0)


@st.dialog('Adicionar arquivos à conversa', width='large')
def upload_dialog(root_path: str):
    """Explica limites, valida uploads e registra sua avaliação inicial."""
    active_root = Path(root_path)
    st.markdown('''
Envie até **10 arquivos por vez**, com no máximo **20 MiB por arquivo**.

Formatos aceitos: **PDF, CSV, JSON, TXT, Markdown, Python, SQL, YAML e TOML**.
Os arquivos são salvos em `inputs/`. Arquivos com qualquer risco detectado podem ser
armazenados e baixados, mas seu conteúdo não será exibido na interface nem retornado por
ferramentas de leitura.
''')
    uploads = st.file_uploader('Selecionar arquivos', type=UPLOAD_TYPES,
                               accept_multiple_files=True, key='chat_upload_' + active_root.name)
    if st.button('Enviar arquivos', type='primary', disabled=not uploads,
                 key='save_upload_' + active_root.name):
        if len(uploads) > MAX_UPLOAD_FILES:
            st.error(f'Envie no máximo {MAX_UPLOAD_FILES} arquivos por vez.')
            return
        oversized = [upload.name for upload in uploads if upload.size > MAX_UPLOAD_BYTES]
        if oversized:
            st.error('Arquivos acima de 20 MiB: ' + ', '.join(oversized))
            return
        results = []
        for upload in uploads:
            path = active_root / 'inputs' / f'{uuid4().hex[:8]}_{safe_name(upload.name)}'
            path.write_bytes(upload.getvalue())
            assessment = assess_file(path, 'uploaded_file')
            results.append({'path': str(path.relative_to(active_root)),
                            'risk': assessment['risk'],
                            'alerts': format_alerts(assessment)})
        st.session_state.upload_notice = results
        st.rerun()

if 'root' not in st.session_state:
    st.session_state.root = str(create_session(BASE))
    st.session_state.messages = []
root = Path(st.session_state.root)
(root / 'work').mkdir(exist_ok=True)
meta = session_meta(root)
llm_defaults = {
    'base_url': os.environ.get('LLAMA_BASE_URL', 'http://127.0.0.1:8080/v1'),
    'model': os.environ.get('LLAMA_MODEL', 'qwen'),
    'max_tokens': 4096,
    'temperature': 0.2,
}
saved_llm = session_settings(root, llm_defaults)
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
        base_url = st.text_input('API do llama.cpp', saved_llm['base_url'],
                                 key='base_url_' + root.name)
        model = st.text_input('Modelo / alias', saved_llm['model'],
                              key='model_' + root.name)
        max_tokens = st.number_input('Máximo de tokens por resposta', min_value=128,
                                     max_value=16384, value=int(saved_llm['max_tokens']), step=128,
                                     key='max_tokens_' + root.name)
        temperature = st.slider('Temperatura', 0.0, 1.0, float(saved_llm['temperature']), 0.1,
                                key='temperature_' + root.name)
        save_session_settings(root, {
            'base_url': base_url.strip(), 'model': model.strip(),
            'max_tokens': int(max_tokens), 'temperature': float(temperature),
        })
        st.caption('Estes parâmetros são salvos automaticamente nesta conversa.')
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
    st.subheader('Converter documentos')
    st.caption('Adicione arquivos pelo botão + ao lado da mensagem. PDFs sem texto exigem OCR adicional.')
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
            validation = job.get('validation')
            projection = validation and validation.get('projection')
            st.markdown('#### Validação')
            if not projection:
                st.caption('Contrato congelado; aguardando uma versão de resultado para avaliação.')
            else:
                summary = {
                    'acceptance_status': projection['acceptance_status'],
                    'verification_status': projection['verification_status'],
                    'approved': projection['approved'],
                    'approval_basis': projection['approval_basis'],
                    'human_review_status': projection['human_review_status'],
                }
                st.json(summary)
                decisions = validation.get('human_decisions', {})
                for criterion_id, criterion in projection['criteria'].items():
                    st.markdown(f"**{criterion['description']}**")
                    st.caption(
                        f"Aceitação: {criterion['effective_status']} · "
                        f"Verificação: {criterion['verification_status']} · "
                        f"Efeito humano: {criterion.get('decision_effect') or 'nenhum'}")
                    accept_col, reject_col, defer_col = st.columns(3)
                    if accept_col.button('Aceitar', key=f'accept_{job["job_id"]}_{criterion_id}'):
                        queue.decide_validation(active_root, job['job_id'], criterion_id, 'SET_ACCEPTED')
                        st.rerun()
                    if reject_col.button('Rejeitar', key=f'reject_{job["job_id"]}_{criterion_id}'):
                        queue.decide_validation(active_root, job['job_id'], criterion_id, 'SET_REJECTED')
                        st.rerun()
                    if defer_col.button('Usar evidência',
                            disabled=criterion_id not in decisions,
                            key=f'defer_{job["job_id"]}_{criterion_id}'):
                        queue.decide_validation(active_root, job['job_id'], criterion_id,
                                                'DEFER_TO_EVIDENCE')
                        st.rerun()
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
                st.markdown(message.additional_kwargs.get('display_content', message.content))
        elif isinstance(message, AIMessage):
            if message.content:
                with st.chat_message('assistant'):
                    st.markdown(message.content)
            for call in message.tool_calls:
                with st.expander('🔧 ' + call['name']):
                    st.json(call['args'])
        elif isinstance(message, ToolMessage):
            with st.expander('Resultado da ferramenta'):
                try:
                    tool_result = json.loads(message.content)
                except (ValueError, TypeError):
                    tool_result = None
                assessment = tool_result.get('security') if isinstance(tool_result, dict) else None
                if assessment and not safe_to_display(assessment):
                    st.warning('Conteúdo ocultado devido a risco de segurança: ' + assessment['risk'])
                    visible = dict(tool_result)
                    visible.pop('text', None)
                    if isinstance(visible.get('matches'), list):
                        visible['matches'] = [dict(item, text=None) if 'text' in item else item
                                              for item in visible['matches']]
                    st.json(visible)
                else:
                    st.code(str(message.content), language='json')
    # Perguntas de ask_user recebem um cartão próprio com alternativa livre condicional.
    for previous in reversed(st.session_state.messages):
        if isinstance(previous, HumanMessage):
            break
        if isinstance(previous, ToolMessage) and previous.name == 'run_python':
            try:
                decision = json.loads(previous.content)
                if decision.get('awaiting_approval'):
                    security_id = decision['security_decision_id']
                    approve, reject = st.columns(2)
                    if approve.button('Aprovar esta execução', type='primary',
                                      key='approve_' + security_id):
                        SecurityStore(root).resolve(security_id, True)
                        st.session_state.pending_prompt = (
                            f'Aprovação humana registrada para {security_id}. '
                            f'Execute novamente {decision["script"]} usando esse approval_id e os mesmos parâmetros.')
                        st.rerun()
                    if reject.button('Rejeitar', key='reject_' + security_id):
                        SecurityStore(root).resolve(security_id, False)
                        st.session_state.pending_prompt = (
                            f'Rejeitei a execução associada a {security_id}. Não execute esse script.')
                        st.rerun()
                    break
            except (ValueError, TypeError, KeyError):
                pass
        if isinstance(previous, ToolMessage) and previous.name == 'ask_user':
            try:
                question = json.loads(previous.content)
                options = [_clean_option(option) for option in question.get('options', [])
                           if isinstance(option, str) and option.strip()][:4]
                options = [option for option in options
                           if option and not option.casefold().startswith(('outro', 'outra'))]
                choices = [*options, 'Outros']
                key = previous.tool_call_id
                with st.container(border=True):
                    selected = st.radio('Escolha uma alternativa', choices, index=None,
                                        key='question_choice_' + key,
                                        label_visibility='collapsed')
                    custom = ''
                    if selected == 'Outros':
                        custom = st.text_input('Digite sua resposta', max_chars=2000,
                                               key='question_other_' + key)
                    answer = custom.strip() if selected == 'Outros' else selected
                    if st.button('Enviar resposta', type='primary', disabled=not answer,
                                 key='question_submit_' + key):
                        decision = record_decision(root, question['question'], answer, options)
                        st.session_state.pending_prompt = {
                            'content': (
                                f'Resposta à pergunta "{question["question"]}": {answer}\n\n'
                                f'Essa escolha foi registrada como {decision["id"]}. '
                                'Retome agora o pedido original automaticamente. Se ainda faltar '
                                'informação, faça apenas a próxima pergunta usando ask_user. Caso '
                                'contrário, prossiga com o trabalho e as ferramentas necessárias.'
                            ),
                            'display': answer,
                            'answer_to': question['question'],
                            'decision_answer': answer,
                        }
                        st.rerun()
            except (ValueError, TypeError, KeyError):
                pass
            break
    choices = decision_log(root)
    if choices:
        with st.expander(f'Rastreio de escolhas ({len(choices)})'):
            for choice in reversed(choices):
                st.markdown(f'**{choice["sequence"]}. {choice["answer"]}**  ')
                st.caption(choice['question'])
    cycles = question_cycles(root)
    if cycles:
        current = cycles[-1]
        label = 'em andamento' if current['status'] == 'active' else 'concluído'
        with st.expander(f'Ciclo de perguntas — {label}'):
            st.markdown(current['goal'])
            st.caption(f"{current['questions_asked']} pergunta(s) realizada(s)")
            for criterion in current['definition_of_done']:
                st.markdown('- ' + criterion)
    notice = st.session_state.pop('upload_notice', None)
    if notice:
        for item in notice:
            if item['risk'] == 'low':
                st.success(f"Arquivo salvo: `{item['path']}`")
            else:
                st.warning(f"Arquivo salvo, mas com conteúdo ocultado: `{item['path']}` (risco {item['risk']}).")
                for alert in item['alerts']:
                    st.caption(alert)
    upload_col, message_col = st.columns([0.08, 0.92], vertical_alignment='bottom')
    with upload_col:
        if st.button('＋', help='Adicionar arquivos', use_container_width=True,
                     key='open_upload_' + root.name):
            upload_dialog(str(root))
    with message_col:
        prompt = st.chat_input('O que vamos investigar?')
    pending = st.session_state.pop('pending_prompt', None)
    submitted = prompt if prompt else pending
    if submitted:
        if isinstance(submitted, dict):
            prompt_content = submitted['content']
            prompt_display = submitted.get('display', prompt_content)
            prompt_metadata = {key: submitted[key] for key in ('answer_to', 'decision_answer')
                               if key in submitted}
        else:
            prompt_content = prompt_display = submitted
            prompt_metadata = {}
        if meta['title'] == 'Nova conversa':
            rename_session(root, ' '.join(prompt_display.split())[:72])
        st.session_state.messages.append(HumanMessage(
            content=prompt_content,
            additional_kwargs={
                **prompt_metadata,
                **({'display_content': prompt_display} if prompt_display != prompt_content else {}),
            }))
        save_messages(root, st.session_state.messages)
        with st.chat_message('user'):
            st.markdown(prompt_display)
        scroll_to_latest()
        try:
            graph = build_agent(root, base_url.strip(), model.strip(), temperature, int(max_tokens))
            with st.status('Consultando Qwen…', expanded=True) as status:
                for update in graph.stream({'messages': st.session_state.messages},
                                           config={'recursion_limit': 24}, stream_mode='updates'):
                    for node, delta in update.items():
                        new_messages = delta.get('messages', [])
                        st.session_state.messages.extend(new_messages)
                        save_messages(root, st.session_state.messages)
                        scroll_to_latest()
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
    scroll_to_latest()

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
            assessment = assess_file(file, 'artifact_preview')
            if (file.suffix.lower() in ('.md','.json','.txt','.py','.sql','.csv','.yaml','.yml','.toml','.log')
                    and safe_to_display(assessment)):
                with file.open('r', encoding='utf-8', errors='replace') as source:
                    text = source.read(15000)
                languages = {'.py':'python','.sql':'sql','.json':'json','.md':'markdown','.csv':'text'}
                st.code(text, language=languages.get(file.suffix.lower(), 'text'))
                if size > 15000:
                    st.caption('Prévia limitada; o download contém o arquivo completo.')
            elif not safe_to_display(assessment):
                st.warning('Prévia ocultada devido a risco de segurança: ' + assessment['risk'])
                for alert in format_alerts(assessment):
                    st.caption(alert)
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
