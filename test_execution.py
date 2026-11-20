"""Testa fila, isolamento de arquivos, protocolo, worker e perguntas sem depender do Qwen."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from storage import create_session, write_text_file, artifacts, rename_session, session_meta
from jobs import JobStore
from agente import build_agent
from worker import extract_outputs, run_job, recover, DockerEngine


def setup_job(tmp_path, key='request-1', timeout=10):
    """Cria uma conversa e enfileira uma tarefa mínima para os testes."""
    root = create_session(tmp_path)
    write_text_file(root, 'work/test.py', 'print("OK")')
    store = JobStore(tmp_path)
    job = store.enqueue(root, 'work/test.py', [], timeout, key)
    return root, store, job


def test_write_code_extension_and_overwrite(tmp_path):
    """Valida extensões, sobrescrita explícita e contenção dos caminhos gravados."""
    root = create_session(tmp_path)
    for name in ('a.py','b.sql','c.json','d.csv'):
        result = write_text_file(root, 'work/sub/' + name, 'conteudo')
        assert result['artifact_path'].endswith(name)
    with pytest.raises(ValueError):
        write_text_file(root, 'work/sub/a.py', 'novo')
    write_text_file(root, 'work/sub/a.py', 'novo', True)
    assert (root / 'work/sub/a.py').read_text() == 'novo'
    assert len(artifacts(root)) == 4
    for name in ('../outside.py','/tmp/a.py','inputs/a.py','work/../../a.py'):
        with pytest.raises(ValueError):
            write_text_file(root, name, 'x')


def test_queue_snapshot_idempotence_and_session_ownership(tmp_path):
    """Confirma imutabilidade, idempotência e isolamento das tarefas por conversa."""
    root, store, job = setup_job(tmp_path)
    write_text_file(root, 'work/test.py', 'print("changed")', True)
    duplicate = store.enqueue(root, 'work/test.py', [], 10, 'request-1')
    assert duplicate['job_id'] == job['job_id']
    frozen = root / 'jobs' / job['job_id'] / 'snapshot/work/test.py'
    assert frozen.read_text() == 'print("OK")'
    with pytest.raises(ValueError):
        store.enqueue(root, 'work/test.py', ['different'], 10, 'request-1')
    other = create_session(tmp_path)
    with pytest.raises(ValueError):
        store.get(other, job['job_id'])
    assert store.cancel(root, job['job_id'])['status'] == 'cancelled'
    assert store.claim() is None
    assert JobStore(tmp_path).get(root, job['job_id'])['status'] == 'cancelled'


def test_snapshot_links_and_limits(tmp_path, monkeypatch):
    """Confirma a rejeição de links simbólicos e snapshots acima do limite."""
    import jobs
    root = create_session(tmp_path)
    write_text_file(root, 'work/main.py', 'pass')
    (root / 'inputs/link').symlink_to('/etc/passwd')
    store = JobStore(tmp_path)
    with pytest.raises(ValueError, match='links'):
        store.enqueue(root, 'work/main.py', [], 2, 'x')
    (root / 'inputs/link').unlink()
    monkeypatch.setattr(jobs, 'MAX_SNAPSHOT_BYTES', 2)
    with pytest.raises(ValueError, match='128 MiB'):
        store.enqueue(root, 'work/main.py', [], 2, 'y')
    assert store.list(root) == []


class FakeProcess:
    """Simula um processo Docker com fluxos de saída em memória."""
    stdout = None
    stderr = None
    def __init__(self):
        """Inicializa os fluxos simulados de saída padrão e erro."""
        self.stdout = io.BytesIO(b'Execution OK\n')
        self.stderr = io.BytesIO(b'')
    def poll(self):
        """Informa que o processo permanece ativo."""
        return None
    def wait(self, timeout=None):
        """Simula o término bem-sucedido do processo."""
        return 0
    def kill(self):
        """Simula o encerramento forçado do processo."""
        pass


class FakeEngine:
    """Simula o mecanismo Docker usado pelo worker."""

    def __init__(self, result=None):
        """Configura o marcador de resultado devolvido ao worker."""
        self.value = result or {'exit_code':0,'timed_out':False}
        self.removed = 0
    def create(self, row, snapshot):
        """Valida a presença do snapshot simulado."""
        assert snapshot.is_dir()
    def start(self, row):
        """Retorna um processo simulado para a tarefa."""
        return FakeProcess()
    def result(self, row):
        """Retorna o marcador de conclusão configurado."""
        return self.value
    def export(self, row, destination):
        """Cria um artefato CSV simulado no destino."""
        destination.mkdir()
        file = destination / 'result.csv'
        file.write_text('x,y\n1,2\n')
        return [file]
    def remove(self, row):
        """Registra a remoção do contêiner simulado."""
        self.removed += 1


@pytest.mark.parametrize('result,status', [({'exit_code':0,'timed_out':False},'succeeded'),
    ({'exit_code':1,'timed_out':False},'failed'), ({'exit_code':None,'timed_out':True},'timed_out')])
def test_worker_result_and_artifact(tmp_path, result, status):
    """Valida a tradução do resultado do runner e a publicação de artefatos."""
    root, store, job = setup_job(tmp_path)
    engine = FakeEngine(result)
    run_job(store, store.claim(), engine)
    final = store.get(root, job['job_id'])
    assert final['status'] == status
    assert final['stdout_tail'] == 'Execution OK\n'
    assert (root / final['artifacts'][0]).is_file()
    assert engine.removed == 1


def test_worker_cancellation(tmp_path):
    """Confirma que um cancelamento solicitado durante a execução é persistido."""
    root, store, job = setup_job(tmp_path)
    row = store.claim()
    class CancelEngine(FakeEngine):
        """Solicita o cancelamento quando o worker consulta o resultado."""

        def result(self, row):
            """Cancela a tarefa e simula uma execução ainda incompleta."""
            store.cancel(root, row['id'])
            return None
    engine = CancelEngine()
    with patch('worker.time.sleep'):
        run_job(store, row, engine)
    assert store.get(root, job['job_id'])['status'] == 'cancelled'
    assert engine.removed == 1


def test_worker_wall_timeout_and_recovery(tmp_path):
    """Valida o prazo do worker e a recuperação de tarefas interrompidas."""
    root, store, job = setup_job(tmp_path)
    row = store.claim()
    engine = FakeEngine()
    with patch('worker.time.monotonic', side_effect=[0, 1000]):
        run_job(store, row, engine)
    assert store.get(root, job['job_id'])['status'] == 'timed_out'
    another = store.enqueue(root, 'work/test.py', [], 2, 'new')
    store.claim()
    recover(store, engine)
    assert store.get(root, another['job_id'])['status'] == 'interrupted'
    assert store.claim() is None


@pytest.mark.parametrize('name,kind', [('../bad', 'file'),('/tmp/bad', 'file'),('link','link')])
def test_tar_rejects_escape_and_links(tmp_path, name, kind):
    """Confirma que a extração rejeita escapes de caminho e links."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        member = tarfile.TarInfo(name)
        if kind == 'link':
            member.type = tarfile.SYMTYPE
            member.linkname = '/etc/passwd'
            archive.addfile(member)
        else:
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
    stream.seek(0)
    with pytest.raises(ValueError):
        extract_outputs(stream, tmp_path / 'out')


def test_docker_flags(tmp_path):
    """Verifica as restrições de isolamento aplicadas ao contêiner."""
    _, store, _ = setup_job(tmp_path)
    row = store.claim()
    engine = DockerEngine()
    with patch.object(engine, 'command') as command:
        engine.create(row, tmp_path / 'snapshot')
    args = command.call_args.args[0]
    for flag in ('--read-only','--network','--memory','--memory-swap','--cpus','--pids-limit','--cap-drop','--tmpfs'):
        assert flag in args
    assert 'none' in args and '/var/run/docker.sock' not in ' '.join(args)


def test_run_python_tool_handoff(tmp_path):
    """Confirma que a ferramenta enfileira a tarefa e encerra o turno."""
    root = create_session(tmp_path)
    write_text_file(root, 'work/test.py', 'print(1)')
    class FakeModel:
        """Modelo simulado que solicita uma execução Python."""

        def __init__(self, **kw):
            """Aceita a mesma configuração do cliente real."""
            pass
        def bind_tools(self, tools):
            """Mantém a interface de associação de ferramentas."""
            return self
        def invoke(self, messages):
            """Solicita a execução do script preparado pelo teste."""
            return AIMessage(content='', tool_calls=[{'id':'call_run', 'name':'run_python',
                'args':{'script':'work/test.py'}, 'type':'tool_call'}])
    with patch('agente.ChatOpenAI', FakeModel):
        result = build_agent(root,'http://localhost:8080/v1','qwen',0,128).invoke(
            {'messages':[HumanMessage(content='Execute')]}, config={'recursion_limit': 8})
    assert 'registrada' in result['messages'][-1].content
    assert len(JobStore(tmp_path).list(root)) == 1


def test_ask_user_pauses_before_other_tools(tmp_path):
    """Confirma que uma pergunta tem prioridade sobre outras ferramentas."""
    root = create_session(tmp_path)
    class FakeModel:
        """Modelo simulado que solicita escrita e esclarecimento juntos."""

        def __init__(self, **kw):
            """Aceita a mesma configuração do cliente real."""
            pass
        def bind_tools(self, tools):
            """Mantém a interface de associação de ferramentas."""
            return self
        def invoke(self, messages):
            """Produz chamadas concorrentes para testar sua priorização."""
            return AIMessage(content='', tool_calls=[
                {'id':'write','name':'write_file','args':{'path':'work/no.py','content':'pass'},'type':'tool_call'},
                {'id':'ask','name':'ask_user','args':{'question':'Qual formato?', 'options':['CSV','JSON']},'type':'tool_call'}])
    with patch('agente.ChatOpenAI', FakeModel):
        result = build_agent(root,'http://localhost:8080/v1','qwen',0,128).invoke(
            {'messages':[HumanMessage(content='Gere um arquivo')]}, config={'recursion_limit': 8})
    assert 'Qual formato?' in result['messages'][-1].content
    assert 'CSV' in result['messages'][-1].content
    assert not (root / 'work/no.py').exists()


def test_names_survive_reload_and_legacy_migration(tmp_path):
    """Valida títulos persistentes e a migração de conversas antigas."""
    root = create_session(tmp_path)
    rename_session(root, 'Meu estudo GNSS')
    assert session_meta(root)['title'] == 'Meu estudo GNSS'
    old = create_session(tmp_path)
    (old / 'conversation.json').write_text(json.dumps([
        {'type':'human','data':{'content':'Primeira pergunta sobre órbitas'}}]))
    assert session_meta(old)['title'] == 'Primeira pergunta sobre órbitas'


def test_actual_runner_success_and_timeout(tmp_path):
    """Executa o runner real e verifica sucesso e estouro de prazo."""
    runner = Path(__file__).parent / 'sandbox/runner.py'
    source = tmp_path / 'snapshot'
    for folder in ('inputs','work','outputs'):
        (source / folder).mkdir(parents=True)
    (source / 'work/main.py').write_text("from pathlib import Path\nprint('OK')\nPath('outputs/result.csv').write_text('a,b\\n1,2')")
    def execute(timeout, work, result):
        """Executa o runner em um subprocesso com caminhos temporários."""
        work.mkdir()
        code = ('import runpy; from pathlib import Path; '
                f"m=runpy.run_path({str(runner)!r}); "
                f"m['execute']('work/main.py', [], {timeout}, Path({str(source)!r}), Path({str(work)!r}), Path({str(result)!r}))")
        return subprocess.run([sys.executable,'-c',code], capture_output=True, text=True, timeout=10)
    result = tmp_path / 'done.json'
    proc = execute(2, tmp_path/'w1', result)
    assert proc.returncode == 0 and 'OK' in proc.stdout
    assert json.loads(result.read_text())['exit_code'] == 0
    assert (tmp_path/'w1/outputs/result.csv').is_file()
    (source / 'work/main.py').write_text('import time; time.sleep(10)')
    proc = execute(1, tmp_path/'w2', result)
    assert proc.returncode == 0
    assert json.loads(result.read_text())['timed_out'] is True


def test_ui_rename_and_download_python(tmp_path, monkeypatch):
    """Valida renomeação, prévia e empacotamento de arquivos pela interface."""
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('HARNESS_DATA_DIR', str(tmp_path))
    app = AppTest.from_file(str(Path(__file__).parent / 'app.py')).run(timeout=30)
    assert not app.exception
    root = Path(app.session_state['root'])
    title = next(t for t in app.text_input if t.label == 'Nome desta conversa')
    title.set_value('Meu laboratório GNSS')
    next(b for b in app.button if b.label == 'Salvar nome').click()
    app.run(timeout=30)
    assert not app.exception
    assert session_meta(root)['title'] == 'Meu laboratório GNSS'
    assert any(t.value == 'Meu laboratório GNSS' for t in app.title)
    write_text_file(root, 'work/analise.py', 'print(42)')
    app.run(timeout=30)
    assert not app.exception
    assert any(s.value == 'work/analise.py' for s in app.selectbox)
    assert any(c.value == 'print(42)' for c in app.code)
    next(b for b in app.button if b.label == 'Preparar ZIP com todos os artefatos').click()
    app.run(timeout=30)
    assert not app.exception
    from zipfile import ZipFile
    with ZipFile(io.BytesIO(app.session_state['archive_' + root.name])) as archive:
        assert archive.read('work/analise.py') == b'print(42)'


def test_ui_question_options_resume_next_turn(tmp_path, monkeypatch):
    """Confirma que uma opção clicada retoma a conversa no turno seguinte."""
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('HARNESS_DATA_DIR', str(tmp_path))
    class FakeModel:
        """Modelo simulado que pergunta o formato antes de responder."""

        def __init__(self, **kw):
            """Aceita a mesma configuração do cliente real."""
            pass
        def bind_tools(self, tools):
            """Mantém a interface de associação de ferramentas."""
            return self
        def invoke(self, messages):
            """Pergunta o formato ou confirma a escolha recebida."""
            if messages[-1].content == 'CSV':
                return AIMessage(content='Formato escolhido: CSV.')
            return AIMessage(content='', tool_calls=[{'id':'question_1','name':'ask_user',
                'args':{'question':'Qual formato deseja?', 'options':['CSV','JSON']},'type':'tool_call'}])
    with patch('agente.ChatOpenAI', FakeModel):
        app = AppTest.from_file(str(Path(__file__).parent / 'app.py')).run(timeout=30)
        app.chat_input[0].set_value('Preciso de um arquivo').run(timeout=30)
        assert not app.exception
        assert any('Qual formato deseja?' in m.value for m in app.markdown)
        next(b for b in app.button if b.label == 'CSV').click()
        app.run(timeout=30)
        assert not app.exception
        assert any('Formato escolhido: CSV.' in m.value for m in app.markdown)


def test_log_limit_and_failed_collection_cleanup(tmp_path, monkeypatch):
    """Valida o limite de logs e a limpeza após falha na coleta."""
    import worker
    root, store, job = setup_job(tmp_path)
    monkeypatch.setattr(worker, 'LOG_LIMIT', 4)
    engine = FakeEngine()
    run_job(store, store.claim(), engine)
    result = store.get(root, job['job_id'])
    assert result['status'] == 'failed'
    assert 'log' in result['error']
    assert (root / result['stdout_path']).stat().st_size == 4
    monkeypatch.setattr(worker, 'LOG_LIMIT', 4*1024*1024)
    next_job = store.enqueue(root, 'work/test.py', [], 10, 'export-error')
    class ExportFailure(FakeEngine):
        """Simula uma falha durante a exportação dos artefatos."""

        def export(self, row, destination):
            """Cria o destino parcial e interrompe a exportação."""
            destination.mkdir()
            raise ValueError('Arquivo não permitido no resultado.')
    engine = ExportFailure()
    run_job(store, store.claim(), engine)
    result = store.get(root, next_job['job_id'])
    assert result['status'] == 'failed' and result['artifacts'] == []
    assert engine.removed == 1
    assert not (root / 'jobs' / next_job['job_id'] / 'collected').exists()
