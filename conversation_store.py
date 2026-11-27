"""Histórico conversacional append-only, ordenado e seguro para múltiplos escritores."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4
from datetime import datetime, timedelta, timezone


class ConversationConflict(RuntimeError):
    """Indica reutilização de identidade com payload diferente."""


class ConversationStore:
    """Usa jobs.sqlite3 como fonte única; JSON é somente exportação legada."""

    def __init__(self, base: Path):
        self.base = Path(base).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.path = self.base / 'jobs.sqlite3'
        for attempt in range(10):
            try:
                with self.db() as db:
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript('''
                CREATE TABLE IF NOT EXISTS conversation_messages (
                  message_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  turn_id TEXT,
                  sequence INTEGER NOT NULL,
                  role TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(session_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS conversation_messages_session
                  ON conversation_messages(session_id, sequence);
                CREATE TABLE IF NOT EXISTS conversation_migrations (
                  session_id TEXT PRIMARY KEY,
                  source_digest TEXT NOT NULL,
                  message_count INTEGER NOT NULL,
                  migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversation_leases (
                  session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                  fencing_generation INTEGER NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                  turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                  kind TEXT NOT NULL, state TEXT NOT NULL,
                  fencing_generation INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  finished_at TEXT, last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS conversation_turns_pending
                  ON conversation_turns(session_id,state,created_at);
                    ''')
                break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() or attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=30000')
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _canonical(entry):
        return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def _role(entry):
        return {'human': 'user', 'ai': 'assistant', 'tool': 'tool', 'system': 'system'}.get(
            entry.get('type'), entry.get('type', 'unknown'))

    def migrate(self, root: Path):
        """Importa uma conversa antiga uma vez e conserva backup para rollback."""
        root = Path(root).resolve()
        session_id = root.name
        history = root / 'conversation.json'
        raw = history.read_bytes() if history.exists() else b'[]'
        digest = sha256(raw).hexdigest()
        try:
            entries = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'Histórico legado inválido: {exc}') from exc
        if not isinstance(entries, list):
            raise ValueError('Histórico legado precisa ser uma lista.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            done = db.execute('SELECT 1 FROM conversation_migrations WHERE session_id=?',
                              (session_id,)).fetchone()
            if done:
                return False
            existing = db.execute('SELECT COUNT(*) FROM conversation_messages WHERE session_id=?',
                                  (session_id,)).fetchone()[0]
            if existing:
                raise ConversationConflict('Sessão possui mensagens sem registro de migração.')
            for index, entry in enumerate(entries, 1):
                original = self._canonical(entry)
                message_id = 'msg_' + sha256(
                    f'{session_id}\0{index}\0{original}'.encode()).hexdigest()[:32]
                entry.setdefault('data', {})['id'] = message_id
                canonical = self._canonical(entry)
                db.execute('''INSERT INTO conversation_messages
                    (message_id,session_id,turn_id,sequence,role,payload)
                    VALUES (?,?,?,?,?,?)''',
                    (message_id, session_id, None, index, self._role(entry), canonical))
            db.execute('''INSERT INTO conversation_migrations
                (session_id,source_digest,message_count) VALUES (?,?,?)''',
                (session_id, digest, len(entries)))
        if history.exists():
            backup = root / 'conversation.pre-sqlite.json'
            if not backup.exists():
                shutil.copyfile(history, backup)
        self.export(root)
        return True

    def append(self, root: Path, messages: list, owner_id: str | None = None,
               fencing_generation: int | None = None):
        """Anexa mensagens ausentes; identidades existentes nunca são sobrescritas."""
        from langchain_core.messages import messages_to_dict
        root = Path(root).resolve()
        self.migrate(root)
        for message in messages:
            if not getattr(message, 'id', None):
                try:
                    message.id = 'msg_' + uuid4().hex
                except (AttributeError, TypeError):
                    pass
        entries = messages_to_dict(messages)
        session_id = root.name
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if owner_id is not None:
                lease = db.execute('''SELECT 1 FROM conversation_leases
                    WHERE session_id=? AND owner_id=? AND fencing_generation=?''',
                    (session_id, owner_id, fencing_generation)).fetchone()
                if not lease:
                    raise ConversationConflict('Escritor perdeu o fencing da conversa.')
            sequence = db.execute('''SELECT COALESCE(MAX(sequence),0)
                FROM conversation_messages WHERE session_id=?''', (session_id,)).fetchone()[0]
            for message, entry in zip(messages, entries):
                canonical = self._canonical(entry)
                message_id = getattr(message, 'id', None)
                if not message_id:
                    raise ValueError('Mensagem sem identidade persistível.')
                existing = db.execute('''SELECT payload FROM conversation_messages
                    WHERE message_id=?''', (message_id,)).fetchone()
                if existing:
                    if existing['payload'] != canonical:
                        raise ConversationConflict(
                            f'Mensagem {message_id} foi reutilizada com conteúdo diferente.')
                    continue
                sequence += 1
                turn_id = getattr(message, 'additional_kwargs', {}).get('turn_id')
                db.execute('''INSERT INTO conversation_messages
                    (message_id,session_id,turn_id,sequence,role,payload)
                    VALUES (?,?,?,?,?,?)''',
                    (message_id, session_id, turn_id, sequence, self._role(entry), canonical))
                if self._role(entry) == 'user' and turn_id:
                    db.execute('''INSERT OR IGNORE INTO conversation_turns
                        (turn_id,session_id,kind,state) VALUES (?,?,'HUMAN','PENDING')''',
                        (turn_id, session_id))
        self.export(root)

    def register_turn(self, session_id: str, turn_id: str, kind: str):
        if kind not in ('HUMAN', 'CONTINUATION'):
            raise ValueError('Tipo de turno inválido.')
        with self.db() as db:
            db.execute('''INSERT OR IGNORE INTO conversation_turns
                (turn_id,session_id,kind,state) VALUES (?,?,?,'PENDING')''',
                (turn_id, session_id, kind))

    def turn_has_assistant_output(self, session_id: str, turn_id: str):
        with self.db() as db:
            row = db.execute('''SELECT 1 FROM conversation_messages
                WHERE session_id=? AND turn_id=? AND role='assistant' LIMIT 1''',
                (session_id, turn_id)).fetchone()
        return row is not None

    def turn_waits_for_user(self, session_id: str, turn_id: str):
        """Reconstrói uma pausa confirmada sem reinferir após crash."""
        with self.db() as db:
            rows = db.execute('''SELECT payload FROM conversation_messages
                WHERE session_id=? AND turn_id=? AND role='tool' ORDER BY sequence''',
                (session_id, turn_id)).fetchall()
        for row in rows:
            entry = json.loads(row['payload'])
            try:
                content = entry['data']['content']
                payload = json.loads(content)
            except (KeyError, TypeError, ValueError):
                continue
            if payload.get('awaiting_user') or payload.get('awaiting_approval'):
                return True
        return False

    def claim_turn(self, session_id: str, turn_id: str, owner_id: str,
                   lease_seconds: int = 1900):
        """Adquire a conversa com geração de fencing e prioriza humanos pendentes."""
        current = datetime.now(timezone.utc)
        expires = (current + timedelta(seconds=lease_seconds)).isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            turn = db.execute('SELECT * FROM conversation_turns WHERE turn_id=? AND session_id=?',
                              (turn_id, session_id)).fetchone()
            if turn is None or turn['state'] not in ('PENDING', 'RETRY_PENDING'):
                return None
            if turn['kind'] == 'CONTINUATION':
                human = db.execute('''SELECT 1 FROM conversation_turns
                    WHERE session_id=? AND kind='HUMAN'
                      AND state IN ('PENDING','RETRY_PENDING') LIMIT 1''',
                    (session_id,)).fetchone()
                if human:
                    return None
            lease = db.execute('SELECT * FROM conversation_leases WHERE session_id=?',
                               (session_id,)).fetchone()
            if lease and lease['expires_at'] > current.isoformat():
                return None
            generation = (int(lease['fencing_generation']) + 1) if lease else 1
            db.execute('''INSERT INTO conversation_leases
                (session_id,owner_id,fencing_generation,expires_at) VALUES (?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET owner_id=excluded.owner_id,
                  fencing_generation=excluded.fencing_generation,expires_at=excluded.expires_at''',
                (session_id, owner_id, generation, expires))
            changed = db.execute('''UPDATE conversation_turns SET state='ACTIVE',
                fencing_generation=? WHERE turn_id=? AND state IN ('PENDING','RETRY_PENDING')''',
                (generation, turn_id))
            if changed.rowcount != 1:
                raise ConversationConflict('Turno mudou durante a reivindicação.')
        return generation

    def finish_turn(self, session_id: str, turn_id: str, owner_id: str,
                    generation: int, error: str | None = None, retry: bool = False):
        """Somente o titular vigente confirma o turno e libera a conversa."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            lease = db.execute('''SELECT 1 FROM conversation_leases
                WHERE session_id=? AND owner_id=? AND fencing_generation=?''',
                (session_id, owner_id, generation)).fetchone()
            if not lease:
                return False
            state = ('RETRY_PENDING' if error and retry else 'ERROR' if error else 'COMPLETED')
            changed = db.execute('''UPDATE conversation_turns SET state=?,finished_at=CURRENT_TIMESTAMP,
                last_error=? WHERE turn_id=? AND fencing_generation=? AND state='ACTIVE' ''',
                (state, error, turn_id, generation))
            db.execute('DELETE FROM conversation_leases WHERE session_id=? AND owner_id=? AND fencing_generation=?',
                       (session_id, owner_id, generation))
        return changed.rowcount == 1

    def pending_human_turn(self, session_id: str):
        with self.db() as db:
            row = db.execute('''SELECT turn_id FROM conversation_turns
                WHERE session_id=? AND kind='HUMAN' AND state IN ('PENDING','RETRY_PENDING')
                ORDER BY created_at,turn_id LIMIT 1''', (session_id,)).fetchone()
        return row['turn_id'] if row else None

    def pending_human_turns(self):
        """Lista entradas humanas que a UI preservou enquanto outro turno estava ativo."""
        with self.db() as db:
            rows = db.execute('''SELECT turn_id,session_id FROM conversation_turns
                WHERE kind='HUMAN' AND state IN ('PENDING','RETRY_PENDING')
                ORDER BY created_at,turn_id''').fetchall()
        return [dict(row) for row in rows]

    def load(self, root: Path):
        """Carrega somente a fonte SQLite em ordem persistida."""
        from langchain_core.messages import messages_from_dict
        root = Path(root).resolve()
        self.migrate(root)
        with self.db() as db:
            rows = db.execute('''SELECT message_id,payload FROM conversation_messages
                WHERE session_id=? ORDER BY sequence''', (root.name,)).fetchall()
        messages = []
        for row in rows:
            message = messages_from_dict([json.loads(row['payload'])])[0]
            message.id = row['message_id']
            messages.append(message)
        return messages

    def export(self, root: Path):
        """Reconstrói JSON de compatibilidade; ele nunca é aceito como escritor após migração."""
        root = Path(root).resolve()
        with self.db() as db:
            rows = db.execute('''SELECT payload FROM conversation_messages
                WHERE session_id=? ORDER BY sequence''', (root.name,)).fetchall()
        content = json.dumps([json.loads(row['payload']) for row in rows],
                             ensure_ascii=False, indent=2)
        target = root / 'conversation.json'
        temporary = root / ('conversation-export-' + uuid4().hex + '.tmp')
        temporary.write_text(content, encoding='utf-8')
        temporary.replace(target)
