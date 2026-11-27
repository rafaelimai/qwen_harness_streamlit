"""Fila SQLite e snapshots imutáveis. Não executa comandos no processo da UI."""
from __future__ import annotations
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
                CREATE TABLE IF NOT EXISTS worker_instances (
                  id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at TEXT NOT NULL,
                  last_heartbeat_at TEXT NOT NULL, version TEXT,
                  current_job_id TEXT, current_attempt_id TEXT
                );
                CREATE TABLE IF NOT EXISTS execution_observations (
                  id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
                  session_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                  worker_instance_id TEXT, phase TEXT NOT NULL, kind TEXT NOT NULL,
                  detail TEXT, metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS observations_job_time
                  ON execution_observations(job_id, attempt_id, observed_at);
                CREATE TABLE IF NOT EXISTS execution_incidents (
                  id TEXT PRIMARY KEY, episode_key TEXT NOT NULL UNIQUE,
                  session_id TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                  worker_instance_id TEXT, health_status TEXT NOT NULL,
                  trigger_kind TEXT NOT NULL, phase TEXT,
                  opened_at TEXT NOT NULL, updated_at TEXT NOT NULL, resolved_at TEXT,
                  threshold_seconds INTEGER NOT NULL, detail TEXT,
                  snapshot_id TEXT
                );
                CREATE INDEX IF NOT EXISTS incidents_session_time
                  ON execution_incidents(session_id, opened_at DESC);
                CREATE TABLE IF NOT EXISTS diagnostic_snapshots (
                  id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                  captured_at TEXT NOT NULL, status TEXT NOT NULL,
                  relative_path TEXT, sha256 TEXT, warnings TEXT NOT NULL DEFAULT '[]',
                  FOREIGN KEY(incident_id) REFERENCES execution_incidents(id)
                );
                CREATE TABLE IF NOT EXISTS result_publications (
                  job_id TEXT NOT NULL, result_version_id TEXT NOT NULL,
                  attempt_id TEXT NOT NULL, publication_status TEXT NOT NULL,
                  validation_status TEXT NOT NULL, validation_ref TEXT,
                  manifest_digest TEXT NOT NULL, published_at TEXT,
                  last_error TEXT,
                  PRIMARY KEY(job_id,result_version_id),
                  UNIQUE(job_id,attempt_id,manifest_digest)
                );
                CREATE TABLE IF NOT EXISTS outbox_events (
                  event_id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL,
                  kind TEXT NOT NULL, payload TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL,
                  consumed_at TEXT, UNIQUE(kind,aggregate_id)
                );
                CREATE TABLE IF NOT EXISTS continuation_requests (
                  continuation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                  source_event_id TEXT NOT NULL, source_turn_id TEXT NOT NULL,
                  root_turn_id TEXT NOT NULL,
                  job_id TEXT NOT NULL, result_version_id TEXT,
                  state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                  depth INTEGER NOT NULL DEFAULT 0, claimed_generation INTEGER,
                  last_error TEXT, next_attempt_at TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(source_event_id,job_id)
                );
                CREATE INDEX IF NOT EXISTS continuations_state
                  ON continuation_requests(state,created_at);
                CREATE TABLE IF NOT EXISTS tool_operations (
                  operation_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                  ordinal INTEGER NOT NULL, tool_name TEXT NOT NULL,
                  arguments_digest TEXT NOT NULL, operation_key TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL, result_payload TEXT, created_at TEXT NOT NULL,
                  UNIQUE(turn_id,ordinal)
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
            continuation_columns = {
                row['name'] for row in db.execute('PRAGMA table_info(continuation_requests)')}
            if 'next_attempt_at' not in continuation_columns:
                db.execute('ALTER TABLE continuation_requests ADD COLUMN next_attempt_at TEXT')
            if 'root_turn_id' not in continuation_columns:
                db.execute('ALTER TABLE continuation_requests ADD COLUMN root_turn_id TEXT')
                db.execute('UPDATE continuation_requests SET root_turn_id=source_turn_id')
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

    def register_worker(self, worker_instance_id: str, pid: int, version: str = '1'):
        """Registra uma instância inequívoca do processo worker."""
        timestamp = now()
        with self.db() as db:
            db.execute('''INSERT OR REPLACE INTO worker_instances
                (id,pid,started_at,last_heartbeat_at,version,current_job_id,current_attempt_id)
                VALUES (?,?,?,?,?,NULL,NULL)''',
                (worker_instance_id, pid, timestamp, timestamp, version))

    def heartbeat_instance(self, worker_instance_id: str, job_id=None, attempt_id=None):
        """Atualiza apenas vitalidade e associação; não declara progresso."""
        with self.db() as db:
            changed = db.execute('''UPDATE worker_instances SET last_heartbeat_at=?,
                current_job_id=?, current_attempt_id=? WHERE id=?''',
                (now(), job_id, attempt_id, worker_instance_id))
        return changed.rowcount == 1

    def observe(self, job_id: str, attempt_id: str, phase: str, kind: str,
                worker_instance_id: str | None = None, detail: str | None = None,
                metadata: dict | None = None, observed_at: str | None = None):
        """Anexa telemetria histórica sem alterar o estado vigente da tarefa."""
        if kind not in {'PHASE', 'ACTIVITY', 'PROGRESS', 'TERMINAL', 'ABANDONED'}:
            raise ValueError('Tipo de observação inválido.')
        with self.db() as db:
            row = db.execute('SELECT session_id FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise ValueError('Tarefa não encontrada.')
            event_id = 'obs_' + uuid4().hex
            db.execute('''INSERT INTO execution_observations
                (id,observed_at,session_id,job_id,attempt_id,worker_instance_id,phase,kind,detail,metadata)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (event_id, observed_at or now(), row['session_id'], job_id, attempt_id,
                 worker_instance_id, phase, kind, detail,
                 json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)))
        return event_id

    def observability(self, root: Path):
        """Retorna a projeção operacional persistida da conversa."""
        session_id = self.session(root)
        with self.db() as db:
            observations = db.execute('''SELECT o.* FROM execution_observations o
                JOIN (SELECT job_id,attempt_id,MAX(observed_at) latest
                      FROM execution_observations WHERE session_id=? GROUP BY job_id,attempt_id) x
                ON o.job_id=x.job_id AND o.attempt_id=x.attempt_id AND o.observed_at=x.latest
                ORDER BY o.observed_at DESC''', (session_id,)).fetchall()
            incidents = db.execute('''SELECT * FROM execution_incidents
                WHERE session_id=? ORDER BY opened_at DESC LIMIT 100''', (session_id,)).fetchall()
            continuations = db.execute('''SELECT * FROM continuation_requests
                WHERE session_id=? ORDER BY created_at DESC LIMIT 100''', (session_id,)).fetchall()
        return {
            'observations': [{**dict(row), 'metadata': json.loads(row['metadata'])}
                             for row in observations],
            'incidents': [dict(row) for row in incidents],
            'continuations': [dict(row) for row in continuations],
        }

    def active_attempts(self):
        """Expõe tentativas vigentes e seus últimos sinais ao supervisor."""
        with self.db() as db:
            rows = db.execute('''SELECT j.*,
                (SELECT MAX(observed_at) FROM execution_observations o
                 WHERE o.job_id=j.id AND o.attempt_id=j.active_token) AS last_activity_at,
                (SELECT MAX(observed_at) FROM execution_observations o
                 WHERE o.job_id=j.id AND o.attempt_id=j.active_token AND o.kind='PROGRESS') AS last_progress_at,
                (SELECT phase FROM execution_observations o
                 WHERE o.job_id=j.id AND o.attempt_id=j.active_token
                 ORDER BY observed_at DESC LIMIT 1) AS phase,
                (SELECT worker_instance_id FROM execution_observations o
                 WHERE o.job_id=j.id AND o.attempt_id=j.active_token
                 ORDER BY observed_at DESC LIMIT 1) AS worker_instance_id,
                (SELECT wi.pid FROM worker_instances wi WHERE wi.id=(
                   SELECT worker_instance_id FROM execution_observations o
                   WHERE o.job_id=j.id AND o.attempt_id=j.active_token
                   ORDER BY observed_at DESC LIMIT 1)) AS pid,
                (SELECT wi.last_heartbeat_at FROM worker_instances wi WHERE wi.id=(
                   SELECT worker_instance_id FROM execution_observations o
                   WHERE o.job_id=j.id AND o.attempt_id=j.active_token
                   ORDER BY observed_at DESC LIMIT 1)) AS last_heartbeat_at
                FROM jobs j WHERE j.status IN ('starting','running')''').fetchall()
        return [dict(row) for row in rows]

    def open_incident(self, row: dict, health_status: str, trigger_kind: str,
                      threshold_seconds: int, detail: str):
        """Abre ou atualiza um episódio de forma idempotente."""
        episode = f"{row['id']}:{row['active_token']}:{trigger_kind}"
        timestamp = now()
        with self.db() as db:
            existing = db.execute('''SELECT id FROM execution_incidents
                WHERE episode_key=? AND resolved_at IS NULL''',
                                  (episode,)).fetchone()
            if existing:
                db.execute('''UPDATE execution_incidents SET health_status=?,updated_at=?,detail=?
                    WHERE id=? AND resolved_at IS NULL''',
                    (health_status, timestamp, detail, existing['id']))
                return existing['id'], False
            if db.execute('SELECT 1 FROM execution_incidents WHERE episode_key=?',
                          (episode,)).fetchone():
                episode += ':' + uuid4().hex
            incident_id = 'inc_' + uuid4().hex
            db.execute('''INSERT INTO execution_incidents
                (id,episode_key,session_id,job_id,attempt_id,worker_instance_id,
                 health_status,trigger_kind,phase,opened_at,updated_at,threshold_seconds,detail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (incident_id, episode, row['session_id'], row['id'], row['active_token'],
                 row.get('worker_instance_id'), health_status, trigger_kind, row.get('phase'),
                 timestamp, timestamp, threshold_seconds, detail))
        return incident_id, True

    def attach_snapshot(self, incident_id: str, relative_path: str | None,
                        digest: str | None, warnings: list[str]):
        """Persiste a referência de um snapshot completo ou parcial."""
        snapshot_id = 'snap_' + uuid4().hex
        status = 'PARTIAL' if warnings else 'COMPLETE'
        with self.db() as db:
            db.execute('''INSERT INTO diagnostic_snapshots
                (id,incident_id,captured_at,status,relative_path,sha256,warnings)
                VALUES (?,?,?,?,?,?,?)''',
                (snapshot_id, incident_id, now(), status, relative_path, digest,
                 json.dumps(warnings, ensure_ascii=False)))
            db.execute('UPDATE execution_incidents SET snapshot_id=?,updated_at=? WHERE id=?',
                       (snapshot_id, now(), incident_id))
        return snapshot_id

    def resolve_incidents(self):
        """Resolve incidentes quando a tentativa termina ou progride após sua abertura."""
        timestamp = now()
        with self.db() as db:
            changed = db.execute('''UPDATE execution_incidents SET health_status='RESOLVED',
                resolved_at=?,updated_at=? WHERE resolved_at IS NULL AND (
                  EXISTS (SELECT 1 FROM jobs j WHERE j.id=job_id AND
                          (j.status IN ('succeeded','failed','cancelled','timed_out','interrupted')
                           OR j.active_token<>attempt_id))
                  OR EXISTS (SELECT 1 FROM execution_observations o WHERE o.job_id=job_id
                             AND o.attempt_id=attempt_id AND o.kind='PROGRESS'
                             AND o.observed_at>opened_at))''', (timestamp, timestamp))
        return changed.rowcount

    def register_continuation(self, root: Path, job_id: str, source_event_id: str,
                              source_turn_id: str, depth: int = 0,
                              root_turn_id: str | None = None):
        """Cria uma intenção única para um handoff efetivamente enfileirado."""
        session_id = self.session(root)
        timestamp = now()
        continuation_id = 'cont_' + sha256(
            f'{session_id}\0{source_event_id}\0{job_id}'.encode()).hexdigest()[:32]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT session_id FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['session_id'] != session_id:
                raise ValueError('Tarefa não pertence à conversa da continuação.')
            db.execute('''INSERT OR IGNORE INTO continuation_requests
                (continuation_id,session_id,source_event_id,source_turn_id,root_turn_id,job_id,state,
                 depth,created_at,updated_at) VALUES (?,?,?,?,?,?,'WAITING_FOR_JOB',?,?,?)''',
                (continuation_id, session_id, source_event_id, source_turn_id,
                 root_turn_id or source_turn_id, job_id,
                 depth, timestamp, timestamp))
            existing = db.execute('SELECT * FROM continuation_requests WHERE continuation_id=?',
                                  (continuation_id,)).fetchone()
        return dict(existing)

    def enforce_automation_budget(self, root_turn_id: str, depth: int):
        """Impõe os limites aprovados sem reduzir a autonomia dentro deles."""
        if depth <= 0:
            return
        if depth >= 5:
            raise ValueError('Limite de profundidade automática (5) atingido.')
        with self.db() as db:
            row = db.execute('''SELECT COUNT(*) count,MIN(created_at) started
                FROM continuation_requests WHERE root_turn_id=?''', (root_turn_id,)).fetchone()
        if int(row['count']) >= 10:
            raise ValueError('Limite de 10 tarefas automáticas por objetivo atingido.')
        if row['started'] and (datetime.now(timezone.utc) - instant(row['started'])).total_seconds() > 7200:
            raise ValueError('Prazo de 2 horas do objetivo automático atingido.')

    def prepare_operation(self, turn_id: str, ordinal: int, tool_name: str, arguments: dict):
        """Reserva uma operação interna antes do efeito e detecta replay divergente."""
        canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'))
        digest = sha256(canonical.encode()).hexdigest()
        operation_key = f'{turn_id}:{ordinal}'
        operation_id = 'op_' + sha256(operation_key.encode()).hexdigest()[:32]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM tool_operations WHERE operation_key=?',
                                  (operation_key,)).fetchone()
            if existing:
                if existing['tool_name'] != tool_name or existing['arguments_digest'] != digest:
                    raise ValueError('Replay do turno propôs operação diferente no mesmo ordinal.')
                row = dict(existing)
                row['result'] = (json.loads(row['result_payload'])
                                 if row['result_payload'] else None)
                return row
            db.execute('''INSERT INTO tool_operations
                (operation_id,turn_id,ordinal,tool_name,arguments_digest,operation_key,status,created_at)
                VALUES (?,?,?,?,?,?,'PLANNED',?)''',
                (operation_id, turn_id, ordinal, tool_name, digest, operation_key, now()))
        return {'operation_id': operation_id, 'operation_key': operation_key,
                'status': 'PLANNED', 'result': None}

    def complete_operation(self, operation_id: str, result: dict):
        """Confirma o resultado interno; repetições devolvem o mesmo payload."""
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tool_operations WHERE operation_id=?',
                             (operation_id,)).fetchone()
            if not row:
                raise ValueError('Operação inexistente.')
            if row['status'] == 'COMPLETED':
                if row['result_payload'] != payload:
                    raise ValueError('Resultado divergente para operação concluída.')
                return json.loads(row['result_payload'])
            db.execute('''UPDATE tool_operations SET status='COMPLETED',result_payload=?
                WHERE operation_id=? AND status='PLANNED' ''', (payload, operation_id))
        return result

    def publish_result_ready(self, job_id: str, attempt_id: str, validation: dict):
        """Publica barreira e outbox juntas, somente para o resultado vigente."""
        result_version_id = validation.get('result_version_id')
        manifest = validation.get('manifest')
        projection = validation.get('projection')
        if not result_version_id or manifest is None or projection is None:
            raise ValueError('Validação ainda não produziu uma versão consumível.')
        canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
        manifest_digest = 'sha256:' + sha256(canonical.encode()).hexdigest()
        validation_status = projection.get('verification_status', 'UNVERIFIED')
        timestamp = now()
        event_id = 'out_' + sha256(
            f'{job_id}\0{result_version_id}\0JOB_RESULT_AVAILABLE'.encode()).hexdigest()[:32]
        # Conferência de arquivos ocorre fora da transação; o estado é revalidado no commit.
        with self.db() as db:
            preflight = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if (preflight is None or preflight['active_token'] != attempt_id
                or preflight['status'] not in TERMINAL):
            return False
        session_root = self.base / preflight['session_id']
        for artifact in manifest.get('artifacts', []):
            path = (session_root / artifact['uri']).resolve()
            if (not path.is_relative_to(session_root.resolve()) or not path.is_file()
                    or path.is_symlink()):
                raise ValueError('Artefato do manifesto está ausente ou fora da conversa.')
            expected = artifact.get('digest', '')
            actual = 'sha256:' + sha256(path.read_bytes()).hexdigest()
            if expected != actual:
                raise ValueError('Integridade do artefato diverge do manifesto publicado.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if (job is None or job['active_token'] != attempt_id
                    or job['status'] not in TERMINAL
                    or job['artifacts'] != preflight['artifacts']):
                return False
            db.execute('''INSERT OR IGNORE INTO result_publications
                (job_id,result_version_id,attempt_id,publication_status,validation_status,
                 validation_ref,manifest_digest,published_at)
                VALUES (?,?,?,'READY',?,?,?,?)''',
                (job_id, result_version_id, attempt_id, validation_status,
                 validation.get('assessment_id'), manifest_digest, timestamp))
            payload = json.dumps({
                'job_id': job_id, 'session_id': job['session_id'],
                'attempt_id': attempt_id, 'result_version_id': result_version_id,
                'execution_status': job['status'], 'validation_status': validation_status,
            }, ensure_ascii=False, sort_keys=True)
            waiting = db.execute('''SELECT 1 FROM continuation_requests
                WHERE job_id=? AND state='WAITING_FOR_JOB' ''', (job_id,)).fetchone()
            if waiting:
                db.execute('''INSERT OR IGNORE INTO outbox_events
                    (event_id,aggregate_id,kind,payload,status,created_at)
                    VALUES (?,?, 'JOB_RESULT_AVAILABLE',?,'PENDING',?)''',
                    (event_id, result_version_id, payload, timestamp))
            db.execute('''UPDATE continuation_requests SET state='RESUME_PENDING',
                result_version_id=?,updated_at=?
                WHERE job_id=? AND state='WAITING_FOR_JOB' ''',
                (result_version_id, timestamp, job_id))
        return True

    def reconcile_results(self, unavailable_after_seconds: int = 30):
        """Repara a janela entre terminal operacional e publicação da validação."""
        with self.db() as db:
            rows = [dict(row) for row in db.execute('''SELECT j.* FROM jobs j
                WHERE j.status IN ('succeeded','failed','cancelled','timed_out','interrupted')
                AND EXISTS (SELECT 1 FROM continuation_requests c WHERE c.job_id=j.id
                            AND c.state='WAITING_FOR_JOB')''')]
        reconciled = 0
        for row in rows:
            try:
                validation = self.validation.get(row['id'])
                if validation and validation.get('result_version_id'):
                    reconciled += bool(self.publish_result_ready(
                        row['id'], row['active_token'], validation))
                    continue
                age = (datetime.now(timezone.utc) - instant(row['finished_at'])).total_seconds()
                if age < unavailable_after_seconds:
                    continue
                self._publish_validation_unavailable(row, 'Validação ausente após o prazo de reconciliação.')
                reconciled += 1
            except Exception as exc:
                age = (datetime.now(timezone.utc) - instant(row['finished_at'])).total_seconds()
                if age >= unavailable_after_seconds:
                    self._publish_validation_unavailable(row, str(exc)[:1000])
                    reconciled += 1
        return reconciled

    def _publish_validation_unavailable(self, job: dict, error: str):
        """Libera resultado terminal com indisponibilidade explícita, nunca silenciosa."""
        result_id = 'unavailable_' + sha256(
            f"{job['id']}\0{job['active_token']}".encode()).hexdigest()[:24]
        manifest_artifacts = []
        session_root = self.base / job['session_id']
        for relative in json.loads(job['artifacts']):
            artifact = (session_root / relative).resolve()
            if (not artifact.is_relative_to(session_root.resolve()) or not artifact.is_file()
                    or artifact.is_symlink()):
                raise ValueError('Artefato indisponível durante reconciliação.')
            manifest_artifacts.append({
                'uri': relative,
                'digest': 'sha256:' + sha256(artifact.read_bytes()).hexdigest(),
                'size_bytes': artifact.stat().st_size,
            })
        manifest = {'artifacts': manifest_artifacts, 'declared_external_effects': []}
        canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
        timestamp = now()
        event_id = 'out_' + sha256(f'{job["id"]}\0{result_id}'.encode()).hexdigest()[:32]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT active_token,status FROM jobs WHERE id=?',
                                 (job['id'],)).fetchone()
            if (current is None or current['active_token'] != job['active_token']
                    or current['status'] not in TERMINAL):
                return False
            db.execute('''INSERT OR IGNORE INTO result_publications
                (job_id,result_version_id,attempt_id,publication_status,validation_status,
                 manifest_digest,published_at,last_error)
                VALUES (?,?,?,'READY','VALIDATION_UNAVAILABLE',?,?,?)''',
                (job['id'], result_id, job['active_token'],
                 'sha256:' + sha256(canonical.encode()).hexdigest(), timestamp, error))
            payload = json.dumps({'job_id': job['id'], 'session_id': job['session_id'],
                'attempt_id': job['active_token'], 'result_version_id': result_id,
                'execution_status': job['status'],
                'validation_status': 'VALIDATION_UNAVAILABLE'}, sort_keys=True)
            db.execute('''INSERT OR IGNORE INTO outbox_events
                (event_id,aggregate_id,kind,payload,status,created_at)
                VALUES (?,?,'JOB_RESULT_AVAILABLE',?,'PENDING',?)''',
                (event_id, result_id, payload, timestamp))
            db.execute('''UPDATE continuation_requests SET state='RESUME_PENDING',
                result_version_id=?,last_error=?,updated_at=?
                WHERE job_id=? AND state='WAITING_FOR_JOB' ''',
                (result_id, error, timestamp, job['id']))
        return True

    def pending_continuations(self):
        """Lista continuações prontas na ordem durável de criação."""
        timestamp = now()
        with self.db() as db:
            rows = db.execute('''SELECT c.*,o.event_id AS outbox_event_id,o.payload AS event_payload
                FROM continuation_requests c
                JOIN outbox_events o ON o.aggregate_id=c.result_version_id
                  AND o.kind='JOB_RESULT_AVAILABLE'
                WHERE c.state IN ('RESUME_PENDING','RETRY_PENDING')
                  AND (c.next_attempt_at IS NULL OR c.next_attempt_at<=?)
                ORDER BY c.created_at,c.continuation_id''', (timestamp,)).fetchall()
        return [{**dict(row), 'event_payload': json.loads(row['event_payload'])} for row in rows]

    def begin_continuation(self, continuation_id: str, turn_id: str, generation: int):
        """Vincula claim da conversa à continuação por compare-and-swap."""
        with self.db() as db:
            changed = db.execute('''UPDATE continuation_requests SET state='RESUMING',
                attempts=attempts+1,claimed_generation=?,next_attempt_at=NULL,updated_at=?
                WHERE continuation_id=? AND state IN ('RESUME_PENDING','RETRY_PENDING')''',
                (generation, now(), continuation_id))
        return changed.rowcount == 1

    def complete_continuation(self, continuation_id: str, generation: int):
        """Confirma consumo e ACK da outbox somente pela geração vigente."""
        timestamp = now()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT result_version_id,session_id FROM continuation_requests
                WHERE continuation_id=? AND state='RESUMING' AND claimed_generation=?''',
                (continuation_id, generation)).fetchone()
            if not row:
                return False
            db.execute('''UPDATE continuation_requests SET state='COMPLETED',updated_at=?
                WHERE continuation_id=?''', (timestamp, continuation_id))
            db.execute('''UPDATE outbox_events SET status='CONSUMED',consumed_at=?
                WHERE aggregate_id=? AND kind='JOB_RESULT_AVAILABLE' ''',
                (timestamp, row['result_version_id']))
            turn_id = 'turn_' + continuation_id
            db.execute('''UPDATE conversation_turns SET state='COMPLETED',
                finished_at=CURRENT_TIMESTAMP WHERE turn_id=? AND session_id=?
                AND fencing_generation=? AND state='ACTIVE' ''',
                (turn_id, row['session_id'], generation))
            db.execute('''DELETE FROM conversation_leases WHERE session_id=?
                AND fencing_generation=?''', (row['session_id'], generation))
        return True

    def recover_abandoned_continuations(self):
        """Reagenda claims cujo lease morreu; saída já publicada será reconciliada."""
        timestamp = now()
        recovered = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT c.continuation_id,c.session_id,c.claimed_generation
                FROM continuation_requests c
                LEFT JOIN conversation_leases l ON l.session_id=c.session_id
                  AND l.fencing_generation=c.claimed_generation AND l.expires_at>?
                WHERE c.state='RESUMING' AND l.session_id IS NULL''', (timestamp,)).fetchall()
            for row in rows:
                db.execute('''UPDATE continuation_requests SET state='RETRY_PENDING',
                    next_attempt_at=?,last_error='Coordenador anterior perdeu o lease.',updated_at=?
                    WHERE continuation_id=? AND state='RESUMING' ''',
                    (timestamp, timestamp, row['continuation_id']))
                db.execute('''UPDATE conversation_turns SET state='RETRY_PENDING',
                    last_error='Coordenador anterior perdeu o lease.'
                    WHERE turn_id=? AND state='ACTIVE' ''',
                    ('turn_' + row['continuation_id'],))
                recovered.append(row['continuation_id'])
        return recovered

    def pause_continuation_for_user(self, continuation_id: str, generation: int):
        """Consome o resultado, mas preserva que a próxima transição exige humano."""
        timestamp = now()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT result_version_id,session_id FROM continuation_requests
                WHERE continuation_id=? AND state='RESUMING' AND claimed_generation=?''',
                (continuation_id, generation)).fetchone()
            if not row:
                return False
            db.execute('''UPDATE continuation_requests SET state='WAITING_FOR_USER',updated_at=?
                WHERE continuation_id=?''', (timestamp, continuation_id))
            db.execute('''UPDATE outbox_events SET status='CONSUMED',consumed_at=?
                WHERE aggregate_id=? AND kind='JOB_RESULT_AVAILABLE' ''',
                (timestamp, row['result_version_id']))
            db.execute('''UPDATE conversation_turns SET state='COMPLETED',finished_at=CURRENT_TIMESTAMP
                WHERE turn_id=? AND fencing_generation=?''',
                ('turn_' + continuation_id, generation))
            db.execute('DELETE FROM conversation_leases WHERE session_id=? AND fencing_generation=?',
                       (row['session_id'], generation))
        return True

    def resolve_waiting_for_user(self, session_id: str):
        """Fecha pausas depois que uma entrada humana foi efetivamente processada."""
        with self.db() as db:
            changed = db.execute('''UPDATE continuation_requests SET state='COMPLETED',updated_at=?
                WHERE session_id=? AND state='WAITING_FOR_USER' ''', (now(), session_id))
        return changed.rowcount

    def fail_continuation(self, continuation_id: str, generation: int, error: str):
        """Aplica retry limitado; incerteza de efeito nunca deve chegar a este caminho."""
        delays = (1, 5, 20)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT attempts FROM continuation_requests
                WHERE continuation_id=? AND state='RESUMING' AND claimed_generation=?''',
                (continuation_id, generation)).fetchone()
            if not row:
                return False
            attempts = int(row['attempts'])
            if attempts >= len(delays):
                state, next_at = 'CONTINUATION_ERROR', None
            else:
                state = 'RETRY_PENDING'
                next_at = (datetime.now(timezone.utc)
                           + timedelta(seconds=delays[attempts - 1])).isoformat()
            db.execute('''UPDATE continuation_requests SET state=?,next_attempt_at=?,
                last_error=?,updated_at=? WHERE continuation_id=?''',
                (state, next_at, error[:2000], now(), continuation_id))
        return state

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
