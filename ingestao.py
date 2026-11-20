"""Conversão determinística: preserva fonte, marca páginas e registra metadados."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import requests
from langgraph.graph import StateGraph, START, END
from typing import TypedDict
from storage import inside


class IngestionState(TypedDict, total=False):
    """Estado compartilhado pelas etapas do grafo de ingestão documental."""
    source: str
    kind: str
    output: str
    markdown_path: str
    warnings: list[str]


def build_ingestion(root: Path):
    """Monta o grafo que converte PDFs e páginas HTML em Markdown rastreável."""

    def prepare(state):
        """Cria o diretório exclusivo que receberá os arquivos convertidos."""
        out = root / 'outputs' / uuid4().hex[:12]
        out.mkdir()
        return {'output': str(out), 'warnings': []}

    def pdf(state):
        """Extrai o texto de um PDF por página e preserva o arquivo original."""
        import shutil
        import pymupdf
        import pymupdf4llm
        source = inside(root, state['source'])
        if source.suffix.lower() != '.pdf' or not source.is_file():
            raise ValueError('Escolha um PDF enviado para inputs/.')
        out = Path(state['output'])
        shutil.copy2(source, out / 'original.pdf')
        chunks, warnings = [], []
        with pymupdf.open(source) as doc:
            if doc.needs_pass:
                raise ValueError('PDF protegido por senha; envie uma cópia desbloqueada.')
            for i in range(len(doc)):
                if not doc[i].get_text().strip():
                    warnings.append(f'Página {i+1}: sem camada de texto; requer OCR.')
                    text = '[Página sem texto extraível. OCR não executado.]'
                else:
                    text = pymupdf4llm.to_markdown(doc, pages=[i])
                chunks.append(f'<!-- página {i+1} -->\n\n{text}')
        dest = out / 'documento.md'
        dest.write_text('\n\n'.join(chunks), encoding='utf-8')
        return {'markdown_path': str(dest.relative_to(root)), 'warnings': warnings}

    def web(state):
        """Baixa uma página HTML limitada e extrai seu conteúdo como Markdown."""
        import trafilatura
        from urllib.parse import urlparse
        url = state['source']
        if urlparse(url).scheme not in ('http', 'https'):
            raise ValueError('Use uma URL http ou https.')
        out = Path(state['output'])
        with requests.get(url, stream=True, timeout=(15, 90), headers={'User-Agent': 'QwenHomelab/1.0'}) as response:
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').lower()
            if content_type and not any(t in content_type for t in ('html', 'text/plain', 'xhtml')):
                raise ValueError('A URL precisa apontar para HTML. Para PDF, faça upload.')
            raw = bytearray()
            for chunk in response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError('Página maior que o limite de 20 MiB.')
            final_url = response.url
        (out / 'original.html').write_bytes(raw)
        text = trafilatura.extract(bytes(raw), url=final_url, output_format='markdown',
                                   include_links=True, include_tables=True,
                                   include_comments=False, with_metadata=True)
        if not text:
            raise ValueError('Sem conteúdo extraível; a página pode exigir JavaScript ou login.')
        dest = out / 'documento.md'
        dest.write_text(f'Fonte: {final_url}\n\n{text}', encoding='utf-8')
        return {'markdown_path': str(dest.relative_to(root))}

    def register(state):
        """Registra a origem, os avisos e o tamanho do documento convertido."""
        metadata = {'source': state['source'], 'markdown_path': state['markdown_path'],
                    'captured_at': datetime.now(timezone.utc).isoformat(),
                    'warnings': state.get('warnings', []),
                    'size_bytes': inside(root, state['markdown_path']).stat().st_size}
        (Path(state['output']) / 'metadata.json').write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        return {}

    graph = StateGraph(IngestionState)
    graph.add_node('prepare', prepare)
    graph.add_node('pdf', pdf)
    graph.add_node('web', web)
    graph.add_node('register', register)
    graph.add_edge(START, 'prepare')
    graph.add_conditional_edges('prepare', lambda s: s['kind'], {'pdf': 'pdf', 'web': 'web'})
    graph.add_edge('pdf', 'register')
    graph.add_edge('web', 'register')
    graph.add_edge('register', END)
    return graph.compile()
