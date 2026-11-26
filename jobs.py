"""Fila SQLite e snapshots imutáveis. Não executa comandos no processo da UI."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from uuid import uuid4

from storage import inside
from validation import HarnessValidation

TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_FILES = 2000
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_FILES = 1000
LEASE_GRACE_SECONDS = 60


def data_dir():
    """Retorna o diretório persistente configurado para sessões e tarefas."""
    return Path(os.environ.get('HARNESS_DATA_DIR', str(Path(__file__).parent / 'data'))).expanduser().resolve()


def now():
    """Retorna o instante UTC atual em formato ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def instant(value=None):
    """Normaliza um instante UTC injetável para comparações e testes determinísticos."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def snapshot_session(root: Path, target: Path) -> str:
    """Copia apenas arquivos regulares; rejeita symlinks e excedentes antes do enqueue."""
    total = count = 0
    digest = sha256()
    for folder in ('inputs', 'work', 'outputs'):
        source = root / folder
        (target / folder).mkdir(parents=True, exist_ok=True)
        if not source.exists():
            continue
        for parent, dirs, files in os.walk(source, followlinks=False):
            for name in sorted(dirs + files):
                file = Path(parent) / name
                mode = file.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError('Snapshots não aceitam links ou arquivos especiais.')
            for name in sorted(files):
                file = Path(parent) / name
                count += 1
                if count > MAX_SNAPSHOT_FILES:
                    raise ValueError('Snapshot excede 2000 arquivos.')
                relative = file.relative_to(root)
                dest = target / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                # O_NOFOLLOW evita seguir um link trocado entre inspeção e abertura.
                fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as src, dest.open('wb') as dst:
                    if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                        raise ValueError('Entrada não é arquivo regular.')
                    digest.update(str(relative).encode())
                    while chunk := src.read(65536):
                        total += len(chunk)
                        if total > MAX_SNAPSHOT_BYTES:
                            raise ValueError('Snapshot excede 128 MiB; reduza os arquivos desta conversa.')
                        dst.write(chunk)
                        digest.update(chunk)
                dest.chmod(0o644)
    for parent, dirs, _ in os.walk(target):
        Path(parent).chmod(0o755)
    return digest.hexdigest()


class JobStore:
    """Gerencia a fila SQLite, seus snapshots e o estado do worker."""

    def __init__(self, base: Path | None = None):
        """Inicializa o armazenamento e cria o esquema SQLite quando necessário."""
        self.base = (base or data_dir()).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.path = self.base / 'jobs.sqlite3'
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                  request_key TEXT NOT NULL, payload TEXT NOT NULL,
                  snapshot_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                  exit_code INTEGER, error TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
                  active_token TEXT, lease_expires_at TEXT,
                  attempt_number INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(session_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS worker_state (
                  id INTEGER PRIMARY KEY CHECK(id=1), heartbeat TEXT NOT NULL
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            migrations = {
                'active_token': 'ALTER TABLE jobs ADD COLUMN active_token TEXT',
                'lease_expires_at': 'ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT',
                'attempt_number': 'ALTER TABLE jobs ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 0',
            }
            for column, statement in migrations.items():
                if column not in columns:
                    db.execute(statement)
            # Execuções de versões anteriores não possuem lease e não poderiam ser retomadas.
            db.execute("UPDATE jobs SET lease_expires_at=? WHERE status IN ('starting','running') AND lease_expires_at IS NULL",
                       (now(),))
        self.validation = HarnessValidation(self.base / 'validation.sqlite3')

    @contextmanager
    def db(self):
        """Fornece uma transação SQLite com commit ou rollback automático."""
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def session(self, root: Path):
        """Valida uma raiz de conversa e retorna seu identificador."""
        root = root.resolve()
        if root.parent != self.base or not (root / 'inputs').is_dir():
            raise ValueError('Conversa fora do diretório de dados.')
        return root.name

    def enqueue(self, root: Path, script: str, args: list[str], timeout: int, request_key: str,
                security: dict | None = None):
        """Valida e enfileira uma execução com snapshot imutável e idempotência."""
        session_id = self.session(root)
        script_path = inside(root, script)
        relative = script_path.relative_to(root)
        if relative.parts[0] != 'work' or script_path.suffix != '.py' or not script_path.is_file():
            raise ValueError('O script deve existir em work/ e terminar em .py.')
        if not 1 <= timeout <= 600:
            raise ValueError('Timeout permitido: 1 a 600 segundos.')
        if len(args) > 64 or any(not isinstance(a, str) or len(a) > 4096 or '\x00' in a for a in args):
            raise ValueError('Argumentos inválidos ou grandes demais.')
        if shutil.disk_usage(self.base).free < MAX_SNAPSHOT_BYTES + 512*1024*1024:
            raise ValueError('Espaço livre insuficiente: preserve pelo menos 640 MiB antes de enfileirar.')
        if security is not None:
            required = {'decision_id', 'script_sha256', 'policy_version', 'risk'}
            if set(security) != required or not all(isinstance(security[k], str) for k in required):
                raise ValueError('Metadados de segurança inválidos.')
            if sha256(script_path.read_bytes()).hexdigest() != security['script_sha256']:
                raise ValueError('O script mudou depois da avaliação de segurança.')
        payload_data = {'script': str(relative), 'args': args, 'timeout': timeout}
        if security is not None:
            payload_data['security'] = security
        payload = json.dumps(payload_data, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE session_id=? AND request_key=?',
                                  (session_id, request_key)).fetchone()
            if existing:
                if existing['payload'] != payload:
                    raise ValueError('Chave de execução já usada com argumentos diferentes.')
                self.validation.bootstrap_job(existing['id'], session_id)
                return self.describe(dict(existing))
            job_id = 'job_' + uuid4().hex
            directory = root / 'jobs' / job_id
            try:
                digest = snapshot_session(root, directory / 'snapshot')
                if security is not None:
                    frozen = directory / 'snapshot' / relative
                    if sha256(frozen.read_bytes()).hexdigest() != security['script_sha256']:
                        raise ValueError('O snapshot diverge do script aprovado; execução cancelada.')
                (directory / 'logs').mkdir()
                db.execute('''INSERT INTO jobs
                    (id,session_id,request_key,payload,snapshot_sha256,status,created_at)
                    VALUES (?,?,?,?,?,'queued',?)''',
                    (job_id, session_id, request_key, payload, digest, now()))
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        self.validation.bootstrap_job(job_id, session_id)
        return self.get(root, job_id)

    def get(self, root: Path, job_id: str):
        """Obtém uma tarefa pertencente à conversa informada."""
        session_id = self.session(root)
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND session_id=?', (job_id, session_id)).fetchone()
        if row is None:
            raise ValueError('Tarefa não encontrada nesta conversa.')
        return self.describe(dict(row))

    def describe(self, row):
        """Converte uma linha do banco em resposta pública, incluindo caudas de logs."""
        result = {k: row[k] for k in ('id','status','created_at','started_at','finished_at','exit_code','error','snapshot_sha256')}
        result['job_id'] = result.pop('id')
        result['cancel_requested'] = bool(row['cancel_requested'])
        result['request'] = json.loads(row['payload'])
        result['artifacts'] = json.loads(row['artifacts'])
        result['attempt_number'] = row['attempt_number']
        result['validation'] = self.validation.get(row['id'])
        root = self.base / row['session_id']
        for stream in ('stdout','stderr'):
            relative = Path('jobs') / row['id'] / 'attempts' / (row['active_token'] or 'legacy') / 'logs' / f'{stream}.log'
            legacy = Path('jobs') / row['id'] / 'logs' / f'{stream}.log'
            path = root / relative
            if not path.exists() and (root / legacy).exists():
                relative, path = legacy, root / legacy
            if path.exists():
                with path.open('rb') as f:
                    f.seek(max(0, path.stat().st_size - 8000))
                    result[f'{stream}_tail'] = f.read(8000).decode('utf-8', errors='replace')
                result[f'{stream}_path'] = str(relative)
        return result

    def list(self, root: Path):
        """Lista as tarefas mais recentes de uma conversa."""
        session_id = self.session(root)
        with self.db() as db:
            rows = db.execute('SELECT * FROM jobs WHERE session_id=? ORDER BY created_at DESC LIMIT 100',
                              (session_id,)).fetchall()
        return [self.describe(dict(r)) for r in rows]

    def cancel(self, root: Path, job_id: str):
        """Solicita o cancelamento de uma tarefa ainda não terminal."""
        session_id = self.session(root)
        with self.db() as db:
            db.execute('''UPDATE jobs SET cancel_requested=1, status='cancelled', finished_at=?
                WHERE id=? AND session_id=? AND status NOT IN ('succeeded','failed','cancelled','timed_out','interrupted')''',
                (now(), job_id, session_id))
        return self.get(root, job_id)

    def claim(self, at=None, lease_seconds=None):
        """Reserva ou retoma atomicamente uma tarefa e cria um token de tentativa."""
        current = instant(at)
        current_text = current.isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT * FROM jobs
                WHERE cancel_requested=0 AND (
                  status='queued' OR
                  (status IN ('starting','running') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?)
                )
                ORDER BY CASE WHEN status='queued' THEN 0 ELSE 1 END, created_at LIMIT 1''',
                (current_text,)).fetchone()
            if row:
                token = uuid4().hex
                requested = json.loads(row['payload'])['timeout']
                duration = lease_seconds if lease_seconds is not None else requested + LEASE_GRACE_SECONDS
                if duration <= 0:
                    raise ValueError('O lease precisa ser positivo.')
                expires = (current + timedelta(seconds=duration)).isoformat()
                changed = db.execute('''UPDATE jobs SET status='starting', started_at=?, finished_at=NULL,
                    exit_code=NULL, error=NULL, artifacts='[]', active_token=?, lease_expires_at=?,
                    attempt_number=attempt_number+1
                    WHERE id=? AND (status='queued' OR
                      (status IN ('starting','running') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?))''',
                    (current_text, token, expires, row['id'], current_text))
                if changed.rowcount != 1:
                    return None
                claimed = db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone()
                claimed = dict(claimed)
                self.validation.bootstrap_job(claimed['id'], claimed['session_id'])
                self.validation.record_attempt_started(claimed['id'], token)
                return claimed
        return None

    def update_attempt(self, job_id, token, **fields):
        """Atualiza uma tentativa somente se seu token ainda detiver a tarefa."""
        allowed = {'status','started_at','finished_at','exit_code','error','artifacts'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Campos inválidos.')
        with self.db() as db:
            changed = db.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in fields) +
                " WHERE id=? AND active_token=? AND status IN ('starting','running')",
                [*fields.values(), job_id, token])
        return changed.rowcount == 1

    def token_is_active(self, job_id, token):
        """Informa se uma tentativa ainda é a titular, independentemente do prazo do lease."""
        with self.db() as db:
            row = db.execute("SELECT 1 FROM jobs WHERE id=? AND active_token=? AND status IN ('starting','running')",
                             (job_id, token)).fetchone()
        return row is not None

    def heartbeat(self):
        """Registra o instante do último sinal de vida do worker."""
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO worker_state VALUES (1,?)', (now(),))

    def worker_status(self):
        """Descreve a disponibilidade do worker com base no último sinal de vida."""
        with self.db() as db:
            row = db.execute('SELECT heartbeat FROM worker_state WHERE id=1').fetchone()
        if not row:
            return 'Worker ainda não iniciou.'
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['heartbeat'])).total_seconds()
        return 'Worker ativo.' if age < 15 else f'Worker sem sinal há {int(age)} s.'

    def unfinished(self):
        """Retorna tentativas em andamento; a retomada depende do vencimento do lease."""
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status IN ('starting','running')")]

    def decide_validation(self, root: Path, job_id: str, criterion_id: str,
                          action: str, actor_id: str = 'local-user'):
        """Registra uma decisão humana em critério pertencente à conversa."""
        self.get(root, job_id)  # valida isolamento por sessão
        self.validation.decide(job_id, criterion_id, action, actor_id)
        return self.get(root, job_id)
