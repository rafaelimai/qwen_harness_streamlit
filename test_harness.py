"""Testes locais: conversão, isolamento, ciclo real ToolNode e UI sem Qwen."""
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from storage import create_session, inside, artifacts
from ingestao import build_ingestion
from agente import build_agent, complete_pending_calls, make_tools


def test_containment(tmp_path):
    """Valida a contenção de caminhos comuns e links simbólicos."""
    root = create_session(tmp_path)
    with pytest.raises(ValueError):
        inside(root, '../segredo')
    elsewhere = tmp_path / 'outside'
    elsewhere.mkdir()
    (root / 'inputs' / 'link').symlink_to(elsewhere)
    with pytest.raises(ValueError):
        inside(root, 'inputs/link/file.txt')


def test_pdf_pipeline(tmp_path):
    """Confirma a conversão paginada de PDF e o registro de metadados."""
    import pymupdf
    root = create_session(tmp_path)
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72,72), 'GNSS: residual measurement on page one.')
        doc.new_page().insert_text((72,72), 'Second page: GLONASS orbit.')
        doc.save(root / 'inputs' / 'sample.pdf')
    result = build_ingestion(root).invoke({'source': 'inputs/sample.pdf', 'kind': 'pdf'})
    md = inside(root, result['markdown_path']).read_text()
    assert 'GNSS' in md and 'GLONASS' in md and 'página 2' in md
    assert (Path(result['output']) / 'original.pdf').is_file()
    assert json.loads((Path(result['output']) / 'metadata.json').read_text())['size_bytes'] > 0


def test_scanned_pdf_warning(tmp_path):
    """Confirma o aviso emitido para uma página sem texto extraível."""
    import pymupdf
    root = create_session(tmp_path)
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(root / 'inputs' / 'blank.pdf')
    result = build_ingestion(root).invoke({'source': 'inputs/blank.pdf', 'kind': 'pdf'})
    assert len(result['warnings']) == 1


def test_web_pipeline(tmp_path):
    """Valida o download, a preservação e a conversão de uma página HTML."""
    root = create_session(tmp_path)
    html = b'<html><head><title>GNSS study</title></head><body><article><h1>GNSS study</h1><p>' + b'Satellite residuals and ionosphere measurements. ' * 80 + b'</p></article></body></html>'
    class Response:
        """Simula uma resposta HTTP em streaming com conteúdo HTML."""

        headers = {'Content-Type': 'text/html'}
        url = 'https://example.org/study'
        def __enter__(self):
            """Abre o contexto da resposta simulada."""
            return self
        def __exit__(self, *a):
            """Fecha o contexto sem suprimir exceções."""
            pass
        def raise_for_status(self):
            """Representa uma resposta HTTP bem-sucedida."""
            pass
        def iter_content(self, n):
            """Entrega o HTML como um único bloco de conteúdo."""
            yield html
    with patch('ingestao.requests.get', return_value=Response()):
        result = build_ingestion(root).invoke({'source': 'https://example.org/study', 'kind': 'web'})
    assert 'Satellite' in inside(root, result['markdown_path']).read_text()
    assert (Path(result['output']) / 'original.html').read_bytes() == html


def test_graph_tool_cycle(tmp_path):
    """Exercita o ciclo entre modelo, ferramenta e resposta final do grafo."""
    root = create_session(tmp_path)
    class FakeModel:
        """Modelo simulado que grava um relatório por ferramenta."""

        def __init__(self, **kwargs):
            """Aceita a mesma configuração do cliente real."""
            pass
        def bind_tools(self, tools):
            """Mantém a interface de associação de ferramentas."""
            return self
        def invoke(self, messages):
            """Solicita a gravação e confirma o resultado recebido."""
            if isinstance(messages[-1], ToolMessage):
                assert 'artifact_path' in messages[-1].content
                return AIMessage(content='Relatório salvo.')
            return AIMessage(content='', tool_calls=[{'name': 'save_markdown', 'args': {
                'filename': 'analise.md', 'content': '# Resultado\nTeste GNSS'}, 'id': 'call_1', 'type': 'tool_call'}])
    with patch('agente.ChatOpenAI', FakeModel):
        graph = build_agent(root, 'http://localhost:8080/v1', 'qwen', 0, 512)
        result = graph.invoke({'messages': [HumanMessage(content='Gere um relatório')]})
    assert result['messages'][-1].content == 'Relatório salvo.'
    assert len(artifacts(root)) == 1
    tools = {t.name: t for t in make_tools(root)}
    assert tools['search_files'].invoke({'query': 'GNSS'})['matches']
    assert 'Resultado' in tools['read_file'].invoke({'path': str(artifacts(root)[0].relative_to(root))})['text']


def test_interrupted_history():
    """Valida o fechamento de chamadas de ferramenta interrompidas."""
    messages = [AIMessage(content='', tool_calls=[{'id':'x', 'name':'list_files', 'args':{}, 'type':'tool_call'}])]
    repaired = complete_pending_calls(messages)
    assert repaired[0].tool_call_id == 'x'
    assert not complete_pending_calls(messages + repaired)


def test_streamlit_initial_and_chat(tmp_path, monkeypatch):
    """Confirma a inicialização da interface e um turno de conversa simulado."""
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('HARNESS_DATA_DIR', str(tmp_path))
    class FakeGraph:
        """Grafo simulado que transmite uma única resposta do assistente."""

        def stream(self, *a, **kw):
            """Produz uma atualização de modelo para a interface."""
            yield {'model': {'messages': [AIMessage(content='Qwen conectado no teste.')]}}
    with patch('agente.build_agent', return_value=FakeGraph()):
        app = AppTest.from_file(str(Path(__file__).parent / 'app.py')).run(timeout=30)
        assert not app.exception
        app.chat_input[0].set_value('Olá').run(timeout=30)
        assert not app.exception
        assert any('Qwen conectado' in m.value for m in app.markdown)
        assert list(tmp_path.glob('*/conversation.json'))
