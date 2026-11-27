"""Workspace por conversa; caminhos de ferramentas nunca saem dessa raiz."""
import json
import os
import re
from datetime import datetime, timezone
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


def save_messages(root: Path, messages: list, *, owner_id: str | None = None,
                  fencing_generation: int | None = None) -> None:
    """Anexa mensagens ao SQLite; conversation.json é apenas exportação."""
    from conversation_store import ConversationStore
    ConversationStore(root.parent).append(root, messages, owner_id, fencing_generation)


def load_messages(root: Path) -> list:
    """Carrega o histórico da fonte de verdade SQLite."""
    from conversation_store import ConversationStore
    return ConversationStore(root.parent).load(root)


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


def session_settings(root: Path, defaults: dict | None = None) -> dict:
    """Retorna parâmetros persistidos da LLM, completando versões antigas com padrões."""
    data = session_meta(root)
    saved = data.get('llm_settings', {}) if isinstance(data.get('llm_settings'), dict) else {}
    return {**(defaults or {}), **saved}


def save_session_settings(root: Path, settings: dict) -> dict:
    """Persiste atomicamente apenas parâmetros permitidos, sem credenciais."""
    allowed = {'base_url', 'model', 'temperature', 'max_tokens'}
    clean = {key: settings[key] for key in allowed if key in settings}
    data = session_meta(root)
    if data.get('llm_settings') == clean:
        return clean
    data['llm_settings'] = clean
    path = root / 'session.json'
    temp = path.with_name('session-' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
    return clean


def decision_log(root: Path) -> list[dict]:
    """Lê o rastreio cronológico de respostas estruturadas da conversa."""
    path = root / 'decisions.json'
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def record_decision(root: Path, question: str, answer: str, options: list[str]) -> dict:
    """Acrescenta atomicamente uma escolha sem apagar versões ou respostas anteriores."""
    question, answer = question.strip()[:2000], answer.strip()[:2000]
    if not question or not answer:
        raise ValueError('Pergunta e resposta são obrigatórias no rastreio.')
    entries = decision_log(root)
    entry = {
        'id': 'decision_' + uuid4().hex,
        'sequence': len(entries) + 1,
        'question': question,
        'answer': answer,
        'options': [str(option).strip()[:300] for option in options[:4]],
        'answered_at': datetime.now(timezone.utc).isoformat(),
    }
    entries.append(entry)
    path = root / 'decisions.json'
    temp = path.with_name('decisions-' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)
    return entry


def question_cache(root: Path) -> list[dict]:
    """Lê perguntas condicionais pré-calculadas pela LLM para esta conversa."""
    path = root / 'question-cache.json'
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def cache_followups(root: Path, source_question: str, followups: list[dict]) -> None:
    """Substitui atomicamente o cache de ramificações de uma pergunta."""
    entries = [entry for entry in question_cache(root)
               if entry.get('source_question') != source_question]
    for followup in followups[:4]:
        entries.append({
            'source_question': source_question[:2000],
            'answer': str(followup['answer']).strip()[:2000],
            'question': str(followup['question']).strip()[:2000],
            'options': [str(option).strip()[:300] for option in followup.get('options', [])[:4]],
            'created_at': datetime.now(timezone.utc).isoformat(),
        })
    path = root / 'question-cache.json'
    temp = path.with_name('question-cache-' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(entries[-100:], ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def cached_followup(root: Path, source_question: str, answer: str) -> dict | None:
    """Obtém a pergunta pré-calculada para uma resposta exata, sem consumir o registro."""
    source, selected = source_question.strip().casefold(), answer.strip().casefold()
    return next((entry for entry in reversed(question_cache(root))
                 if entry.get('source_question', '').strip().casefold() == source
                 and entry.get('answer', '').strip().casefold() == selected), None)


def question_cycles(root: Path) -> list[dict]:
    """Lê os ciclos de consulta iniciados pela LLM."""
    path = root / 'question-cycles.json'
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_cycles(root: Path, cycles: list[dict]) -> None:
    path = root / 'question-cycles.json'
    temp = path.with_name('question-cycles-' + uuid4().hex + '.tmp')
    temp.write_text(json.dumps(cycles, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def active_question_cycle(root: Path) -> dict | None:
    """Retorna o ciclo de consulta ainda aberto, quando existir."""
    return next((cycle for cycle in reversed(question_cycles(root))
                 if cycle.get('status') == 'active'), None)


def ensure_question_cycle(root: Path, goal: str = '', definition_of_done=None) -> dict:
    """Inicia um ciclo ou registra mais uma pergunta no ciclo ativo."""
    cycles = question_cycles(root)
    active = next((cycle for cycle in reversed(cycles) if cycle.get('status') == 'active'), None)
    if active is None:
        criteria = [str(item).strip()[:500] for item in (definition_of_done or [])
                    if str(item).strip()][:8]
        active = {
            'id': 'cycle_' + uuid4().hex,
            'status': 'active',
            'goal': goal.strip()[:1000] or 'Obter os esclarecimentos necessários para retomar o pedido.',
            'definition_of_done': criteria or [
                'Informações indispensáveis foram respondidas.',
                'Contradições relevantes foram esclarecidas.',
            ],
            'questions_asked': 0,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'completed_at': None,
        }
        cycles.append(active)
    active['questions_asked'] += 1
    _save_cycles(root, cycles)
    return active


def complete_question_cycle(root: Path) -> dict | None:
    """Encerra o ciclo ativo quando a LLM deixa o modo de perguntas."""
    cycles = question_cycles(root)
    active = next((cycle for cycle in reversed(cycles) if cycle.get('status') == 'active'), None)
    if active:
        active['status'] = 'completed'
        active['completed_at'] = datetime.now(timezone.utc).isoformat()
        _save_cycles(root, cycles)
    return active
