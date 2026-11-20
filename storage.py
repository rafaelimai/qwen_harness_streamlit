"""Workspace por conversa; caminhos de ferramentas nunca saem dessa raiz."""
import json
import os
import re
from pathlib import Path
from uuid import uuid4


def inside(root: Path, relative: str) -> Path:
    """Resolve um caminho relativo e garante que ele permaneça dentro da raiz."""
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('O caminho precisa estar dentro desta conversa.')
    return path


def safe_name(name: str) -> str:
    """Converte um nome fornecido pelo usuário em um nome de arquivo seguro."""
    return re.sub(r'[^\w.\-]+', '_', Path(name.replace('\\', '/')).name)[:120] or 'arquivo'


def create_session(base: Path) -> Path:
    """Cria a estrutura de diretórios isolada de uma nova conversa."""
    root = base / uuid4().hex
    (root / 'inputs').mkdir(parents=True)
    (root / 'outputs').mkdir()
    (root / 'work').mkdir()
    return root


def artifacts(root: Path) -> list[Path]:
    """Lista os arquivos regulares produzidos nas áreas de trabalho e saída."""
    return sorted(p for folder in ('outputs', 'work')
                  for p in (root / folder).rglob('*')
                  if p.is_file() and not p.is_symlink()
                  and p.resolve().is_relative_to(root.resolve()))


def save_messages(root: Path, messages: list) -> None:
    """Persiste atomicamente o histórico serializado de uma conversa."""
    from langchain_core.messages import messages_to_dict
    path = root / 'conversation.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(messages_to_dict(messages), ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def write_text_file(root: Path, relative: str, content: str, overwrite: bool = False) -> dict:
    """Grava texto em work/ ou outputs/, mantendo extensão e subpastas."""
    from pathlib import PurePosixPath
    part = PurePosixPath(relative)
    if (part.is_absolute() or '..' in part.parts or not part.parts
            or part.parts[0] not in ('work', 'outputs') or '\\' in relative):
        raise ValueError('Use um caminho relativo em work/ ou outputs/.')
    if len(content.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('Limite de 2 MiB por arquivo textual.')
    path = inside(root, relative)
    # Recusar symlinks, inclusive os que apontam para dentro do workspace.
    cursor = root
    for piece in part.parts:
        cursor = cursor / piece
        if cursor.is_symlink():
            raise ValueError('Gravação não aceita links simbólicos.')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ValueError('Arquivo já existe. Use overwrite=true para substituir.')
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        file.write(content)
    return {'artifact_path': str(path.relative_to(root)), 'size_bytes': path.stat().st_size}


def session_meta(root: Path) -> dict:
    """Migra conversas antigas sem renomear diretórios nem perder referências."""
    from datetime import datetime, timezone
    path = root / 'session.json'
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    title = 'Nova conversa'
    history = root / 'conversation.json'
    if history.exists():
        try:
            entries = json.loads(history.read_text(encoding='utf-8'))
            first = next((m['data']['content'] for m in entries if m['type'] == 'human'), '')
            if isinstance(first, str) and first.strip():
                title = ' '.join(first.split())[:72]
        except (ValueError, KeyError, OSError):
            pass
    data = {'title': title, 'created_at': datetime.fromtimestamp(root.stat().st_mtime, timezone.utc).isoformat()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def rename_session(root: Path, title: str):
    """Atualiza atomicamente o título de uma conversa."""
    title = ' '.join(title.split())[:100]
    if not title:
        raise ValueError('Informe um nome para a conversa.')
    data = session_meta(root)
    data['title'] = title
    path = root / 'session.json'
    temp = path.with_name('session-' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
