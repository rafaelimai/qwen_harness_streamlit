"""Núcleo do contrato de validação 0.2: reducers puros e log SQLite append-only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence
from uuid import uuid4


RUN_STATUSES = {'NOT_STARTED', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED'}
OBSERVATIONS = {'PASS', 'FAIL', 'INCONCLUSIVE'}
TERMINAL_RUN_STATUSES = RUN_STATUSES - {'NOT_STARTED'}
EVIDENCE_POLICIES = {'ALL', 'ANY', 'QUORUM'}
HUMAN_ACTIONS = {'SET_ACCEPTED', 'SET_REJECTED', 'DEFER_TO_EVIDENCE'}


class ValidationError(ValueError):
    """Indica violação estrutural do contrato de validação."""


class ConcurrencyConflict(ValidationError):
    """Indica que a revisão observada de um stream deixou de ser atual."""


class IdempotencyConflict(ValidationError):
    """Indica reutilização de chave idempotente com conteúdo diferente."""


@dataclass(frozen=True)
class ValidatorResult:
    """Resultado operacional e semântico de um validador previsto."""

    validator_id: str
    run_status: str
    observation: str | None = None

    def __post_init__(self):
        if not self.validator_id:
            raise ValidationError('validator_id é obrigatório.')
        if self.run_status not in RUN_STATUSES:
            raise ValidationError(f'Estado operacional inválido: {self.run_status}.')
        if self.run_status == 'COMPLETED':
            if self.observation not in OBSERVATIONS:
                raise ValidationError('COMPLETED exige PASS, FAIL ou INCONCLUSIVE.')
        elif self.observation is not None:
            raise ValidationError(f'{self.run_status} exige observation=null.')


@dataclass(frozen=True)
class EvidencePolicy:
    """Política congelada para combinar validadores de um critério."""

    kind: str = 'ALL'
    required_passes: int | None = None
    blocking_failures: int | None = None

    def validate(self, validator_count: int) -> None:
        if self.kind not in EVIDENCE_POLICIES:
            raise ValidationError(f'Política de evidência inválida: {self.kind}.')
        if self.kind != 'QUORUM':
            if self.required_passes is not None or self.blocking_failures is not None:
                raise ValidationError(f'{self.kind} não aceita limiares de quórum.')
            return
        limits = (self.required_passes, self.blocking_failures)
        if any(not isinstance(value, int) or value < 1 for value in limits):
            raise ValidationError('QUORUM exige limiares inteiros positivos.')
        if any(value > validator_count for value in limits):
            raise ValidationError('Limiar de QUORUM excede o número de validadores.')


@dataclass(frozen=True)
class CriterionProjection:
    """Projeção independente e explicável de um critério."""

    verification_status: str
    automatic_status: str
    effective_status: str
    decision_effect: str | None
    pass_count: int
    fail_count: int
    divergence: bool
    human_reviewed: bool
    human_dependent: bool


@dataclass(frozen=True)
class AssessmentProjection:
    """Agregação global dos critérios obrigatórios."""

    acceptance_status: str
    verification_status: str
    approved: bool
    approval_basis: str
    human_review_status: str
    accepted: int
    rejected: int
    pending: int
    verified: int
    refuted: int
    verification_unverified: int


def _verification(policy: EvidencePolicy, expected: Sequence[str], results: Mapping[str, ValidatorResult]) -> tuple[str, int, int]:
    expected_ids = tuple(expected)
    if len(set(expected_ids)) != len(expected_ids):
        raise ValidationError('A lista de validadores previstos contém duplicatas.')
    policy.validate(len(expected_ids))
    unknown = set(results) - set(expected_ids)
    if unknown:
        raise ValidationError('Resultados de validadores não previstos: ' + ', '.join(sorted(unknown)))
    if not expected_ids:
        return 'UNVERIFIED', 0, 0
    observations = [results[item].observation for item in expected_ids if item in results]
    passes = observations.count('PASS')
    failures = observations.count('FAIL')
    if policy.kind == 'ALL':
        if failures:
            status = 'REFUTED'
        elif passes == len(expected_ids):
            status = 'VERIFIED'
        else:
            status = 'UNVERIFIED'
    elif policy.kind == 'ANY':
        if passes:
            status = 'VERIFIED'
        elif failures:
            status = 'REFUTED'
        else:
            status = 'UNVERIFIED'
    else:
        pass_reached = passes >= policy.required_passes
        fail_reached = failures >= policy.blocking_failures
        if pass_reached == fail_reached:
            status = 'UNVERIFIED'
        else:
            status = 'VERIFIED' if pass_reached else 'REFUTED'
    return status, passes, failures


def project_criterion(expected_validators: Sequence[str], results: Mapping[str, ValidatorResult],
                      policy: EvidencePolicy | None = None, human_action: str | None = None) -> CriterionProjection:
    """Reduz evidências e uma decisão humana corrente sem alterar os fatos originais."""
    policy = policy or EvidencePolicy()
    for validator_id, result in results.items():
        if validator_id != result.validator_id:
            raise ValidationError('A chave do resultado diverge de validator_id.')
    verification, passes, failures = _verification(policy, expected_validators, results)
    automatic = {'VERIFIED': 'ACCEPTED', 'REFUTED': 'REJECTED',
                 'UNVERIFIED': 'PENDING'}[verification]
    if human_action is not None and human_action not in HUMAN_ACTIONS:
        raise ValidationError(f'Ação humana inválida: {human_action}.')
    effective = automatic
    effect = None
    if human_action == 'SET_ACCEPTED':
        effective = 'ACCEPTED'
        effect = {'ACCEPTED': 'HUMAN_CONFIRMATION', 'REJECTED': 'ACCEPT_OVERRIDE',
                  'PENDING': 'MANUAL_ACCEPTANCE'}[automatic]
    elif human_action == 'SET_REJECTED':
        effective = 'REJECTED'
        effect = {'ACCEPTED': 'REJECT_OVERRIDE', 'REJECTED': 'HUMAN_CONFIRMATION',
                  'PENDING': 'MANUAL_REJECTION'}[automatic]
    elif human_action == 'DEFER_TO_EVIDENCE':
        effect = 'NO_ACTIVE_INTERVENTION'
    return CriterionProjection(
        verification, automatic, effective, effect, passes, failures,
        bool(passes and failures), human_action is not None,
        human_action == 'SET_ACCEPTED' and automatic != 'ACCEPTED',
    )


def project_assessment(criteria: Sequence[CriterionProjection]) -> AssessmentProjection:
    """Agrega critérios obrigatórios segundo os dois eixos do contrato 0.2."""
    if not criteria:
        raise ValidationError('Uma avaliação exige ao menos um critério obrigatório.')
    accepted = sum(item.effective_status == 'ACCEPTED' for item in criteria)
    rejected = sum(item.effective_status == 'REJECTED' for item in criteria)
    pending = len(criteria) - accepted - rejected
    acceptance = 'REJECTED' if rejected else ('ACCEPTED' if accepted == len(criteria) else 'PENDING')
    verified = sum(item.verification_status == 'VERIFIED' for item in criteria)
    refuted = sum(item.verification_status == 'REFUTED' for item in criteria)
    unverified = len(criteria) - verified - refuted
    verification = ('REFUTED' if refuted else
                    ('VERIFIED' if verified == len(criteria) else
                     ('PARTIALLY_VERIFIED' if verified else 'UNVERIFIED')))
    approved = acceptance == 'ACCEPTED'
    dependent = sum(item.human_dependent for item in criteria)
    basis = ('NONE' if not approved else 'AUTOMATED_ONLY' if dependent == 0 else
             'HUMAN_ONLY' if dependent == len(criteria) else 'MIXED')
    reviews = sum(item.human_reviewed for item in criteria)
    review_status = 'NONE' if reviews == 0 else ('COMPLETE' if reviews == len(criteria) else 'PARTIAL')
    return AssessmentProjection(acceptance, verification, approved, basis, review_status,
                                accepted, rejected, pending, verified, refuted, unverified)


class EventStore:
    """Log SQLite append-only com ordem global, revisão de stream e idempotência."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = self.db()
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS validation_events (
                  global_position INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT NOT NULL UNIQUE,
                  event_type TEXT NOT NULL,
                  schema_version INTEGER NOT NULL,
                  stream_id TEXT NOT NULL,
                  stream_revision INTEGER NOT NULL,
                  recorded_at TEXT NOT NULL,
                  producer_type TEXT NOT NULL,
                  producer_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  task_id TEXT,
                  payload TEXT NOT NULL,
                  UNIQUE(stream_id, stream_revision),
                  UNIQUE(producer_type, producer_id, idempotency_key)
                );
            ''')
            db.commit()
        finally:
            db.close()

    def db(self):
        """Abre conexão configurada; a transação é controlada pelo chamador."""
        return sqlite3.connect(self.path, timeout=30)

    def append(self, *, event_type: str, stream_id: str, expected_revision: int,
               producer_type: str, producer_id: str, idempotency_key: str,
               payload: Mapping, task_id: str | None = None, schema_version: int = 1) -> dict:
        """Acrescenta um evento ou devolve a repetição idempotente já persistida."""
        if expected_revision < 0 or not all((event_type, stream_id, producer_type,
                                             producer_id, idempotency_key)):
            raise ValidationError('Envelope de evento inválido.')
        canonical = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'))
        connection = self.db()
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('BEGIN IMMEDIATE')
            duplicate = connection.execute('''SELECT * FROM validation_events
                WHERE producer_type=? AND producer_id=? AND idempotency_key=?''',
                (producer_type, producer_id, idempotency_key)).fetchone()
            if duplicate:
                if (duplicate['event_type'] != event_type or duplicate['stream_id'] != stream_id
                        or duplicate['schema_version'] != schema_version
                        or duplicate['task_id'] != task_id or duplicate['payload'] != canonical):
                    raise IdempotencyConflict('Chave idempotente reutilizada com evento diferente.')
                connection.commit()
                return self._event(dict(duplicate))
            current = connection.execute('''SELECT COALESCE(MAX(stream_revision), 0)
                FROM validation_events WHERE stream_id=?''', (stream_id,)).fetchone()[0]
            if current != expected_revision:
                raise ConcurrencyConflict(
                    f'Revisão esperada {expected_revision}; revisão atual {current}.')
            event_id = 'evt_' + uuid4().hex
            recorded_at = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute('''INSERT INTO validation_events
                (event_id,event_type,schema_version,stream_id,stream_revision,recorded_at,
                 producer_type,producer_id,idempotency_key,task_id,payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (event_id, event_type, schema_version, stream_id, current + 1, recorded_at,
                 producer_type, producer_id, idempotency_key, task_id, canonical))
            row = connection.execute('SELECT * FROM validation_events WHERE global_position=?',
                                     (cursor.lastrowid,)).fetchone()
            connection.commit()
            return self._event(dict(row))
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_stream(self, stream_id: str) -> list[dict]:
        """Lê um stream em ordem de revisão."""
        connection = self.db()
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute('''SELECT * FROM validation_events
                WHERE stream_id=? ORDER BY stream_revision''', (stream_id,)).fetchall()
            return [self._event(dict(row)) for row in rows]
        finally:
            connection.close()

    def current_revision(self, stream_id: str) -> int:
        """Retorna a última revisão persistida de um stream."""
        connection = self.db()
        try:
            return connection.execute('''SELECT COALESCE(MAX(stream_revision), 0)
                FROM validation_events WHERE stream_id=?''', (stream_id,)).fetchone()[0]
        finally:
            connection.close()

    @staticmethod
    def _event(row: dict) -> dict:
        row['payload'] = json.loads(row['payload'])
        row['producer'] = {'type': row.pop('producer_type'), 'id': row.pop('producer_id')}
        return row


class HarnessValidation:
    """Projeção persistida do fluxo de validação dos jobs atuais."""

    CRITERIA = (
        {'criterion_id': 'execution_succeeded', 'description': 'A execução terminou com sucesso.',
         'required': True, 'validators': ['job-status'], 'evidence_policy': {'kind': 'ALL'}},
        {'criterion_id': 'result_accepted', 'description': 'O resultado entregue foi aceito pelo usuário.',
         'required': True, 'validators': [], 'evidence_policy': {'kind': 'ALL'}},
    )

    def __init__(self, path: Path):
        self.events = EventStore(path)
        self.path = Path(path)
        db = self.events.db()
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS validation_tasks (
                  task_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL,
                  contract_id TEXT NOT NULL,
                  contract_version INTEGER NOT NULL,
                  frozen_event_id TEXT NOT NULL,
                  result_version_id TEXT,
                  assessment_id TEXT,
                  validation_run_id TEXT,
                  run_generation INTEGER NOT NULL DEFAULT 0,
                  assessment_revision INTEGER NOT NULL DEFAULT 0,
                  manifest TEXT,
                  validator_results TEXT NOT NULL DEFAULT '{}',
                  human_decisions TEXT NOT NULL DEFAULT '{}',
                  projection TEXT,
                  updated_at TEXT NOT NULL
                );
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(validation_tasks)')}
            if 'assessment_revision' not in columns:
                db.execute('''ALTER TABLE validation_tasks
                    ADD COLUMN assessment_revision INTEGER NOT NULL DEFAULT 0''')
            db.commit()
        finally:
            db.close()

    def _append_control(self, task_id: str, event_type: str, key: str, payload: Mapping,
                        expected_revision: int | None = None) -> dict:
        stream = f'task-control:{task_id}'
        return self.events.append(
            event_type=event_type, stream_id=stream,
            expected_revision=(self.events.current_revision(stream) if expected_revision is None
                               else expected_revision),
            producer_type='SERVICE', producer_id='harness-validation',
            idempotency_key=key, payload=payload, task_id=task_id)

    def bootstrap_job(self, task_id: str, session_id: str) -> dict:
        """Congela idempotentemente o contrato mínimo antes de qualquer tentativa."""
        existing = self.get(task_id)
        if existing:
            return existing
        contract_id = 'contract_' + task_id
        event = self._append_control(task_id, 'CONTRACT_FROZEN', f'contract:{task_id}:v1', {
            'contract_id': contract_id, 'contract_version': 1,
            'criteria': list(self.CRITERIA), 'source': 'JOB_DEFAULT_V1',
        })
        db = self.events.db()
        try:
            db.execute('''INSERT OR IGNORE INTO validation_tasks
                (task_id,session_id,contract_id,contract_version,frozen_event_id,updated_at)
                VALUES (?,?,?,?,?,?)''',
                (task_id, session_id, contract_id, 1, event['event_id'],
                 datetime.now(timezone.utc).isoformat()))
            db.commit()
        finally:
            db.close()
        return self.get(task_id)

    def record_attempt_started(self, task_id: str, attempt_token: str) -> dict:
        """Registra a tentativa depois do contrato congelado e antes da execução."""
        task = self.get(task_id)
        if not task:
            raise ValidationError('Contrato da tarefa ainda não foi congelado.')
        stream = f'attempt:{attempt_token}'
        return self.events.append(
            event_type='ATTEMPT_STARTED', stream_id=stream, expected_revision=0,
            producer_type='SERVICE', producer_id='job-queue',
            idempotency_key=f'attempt-start:{attempt_token}', task_id=task_id,
            payload={'task_id': task_id, 'attempt_id': attempt_token,
                     'contract_id': task['contract_id'],
                     'contract_version': task['contract_version'],
                     'contract_frozen_event_id': task['frozen_event_id']})

    def record_job_result(self, task_id: str, attempt_token: str, status: str,
                          artifacts: Sequence[Mapping]) -> dict:
        """Registra, seleciona e avalia uma versão imutável do resultado do job."""
        task = self.get(task_id)
        if not task:
            raise ValidationError('Tarefa de validação inexistente.')
        manifest = {'artifacts': list(artifacts), 'declared_external_effects': []}
        canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
        import hashlib
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        identity_digest = hashlib.sha256(
            f'{task_id}\0{attempt_token}\0{digest}'.encode()).hexdigest()
        result_id = 'result_' + identity_digest[:24]
        if task.get('result_version_id') == result_id and task.get('projection'):
            return task
        attempt_stream = f'attempt:{attempt_token}'
        self.events.append(
            event_type='RESULT_REGISTERED', stream_id=attempt_stream,
            expected_revision=self.events.current_revision(attempt_stream),
            producer_type='SERVICE', producer_id='job-worker',
            idempotency_key=f'result:{attempt_token}', task_id=task_id,
            payload={'result_version_id': result_id, 'manifest': manifest,
                     'manifest_digest': 'sha256:' + digest, 'execution_status': status.upper()})
        previous = task.get('result_version_id')
        self._append_control(task_id, 'RESULT_SELECTED', f'{task_id}:select:{result_id}', {
            'result_version_id': result_id, 'previous_result_version_id': previous,
            'selection_reason': 'WORKER_COMPLETED'})
        assessment_id = 'assessment_' + hashlib.sha256(
            f"{task_id}\0{task['contract_id']}\0{task['contract_version']}\0{result_id}".encode()
        ).hexdigest()[:24]
        self._append_control(task_id, 'ASSESSMENT_ACTIVATED', f'{task_id}:assessment:{result_id}', {
            'assessment_id': assessment_id, 'result_version_id': result_id,
            'activation_outcome': 'REVALIDATION_REQUIRED'})
        generation = int(task.get('run_generation') or 0) + 1
        run_id = 'validation_run_' + hashlib.sha256(
            f'{assessment_id}\0{generation}'.encode()).hexdigest()[:24]
        self._append_control(task_id, 'VALIDATION_RUN_CREATED', f'run:{run_id}', {
            'assessment_id': assessment_id, 'validation_run_id': run_id,
            'run_generation': generation})
        observation = 'PASS' if status == 'succeeded' else 'FAIL'
        run_stream = f'validation-run:{run_id}'
        self.events.append(
            event_type='VALIDATOR_FINISHED', stream_id=run_stream, expected_revision=0,
            producer_type='SERVICE', producer_id='job-validator',
            idempotency_key=f'validator:{run_id}:job-status', task_id=task_id,
            payload={'criterion_id': 'execution_succeeded', 'validator_id': 'job-status',
                     'validator_run_status': 'COMPLETED', 'observation': observation})
        self._append_control(task_id, 'VALIDATION_RUN_SELECTED', f'run-select:{run_id}', {
            'assessment_id': assessment_id, 'validation_run_id': run_id,
            'run_generation': generation})
        results = {'execution_succeeded': {'job-status': {
            'run_status': 'COMPLETED', 'observation': observation}}}
        self._save_result(task_id, result_id, assessment_id, run_id, generation,
                          manifest, results, {})
        return self.get(task_id)

    def decide(self, task_id: str, criterion_id: str, action: str, actor_id: str) -> dict:
        """Registra decisão humana corrente e recalcula a avaliação."""
        if action not in HUMAN_ACTIONS:
            raise ValidationError('Ação humana inválida.')
        task = self.get(task_id)
        if not task or not task.get('validation_run_id'):
            raise ValidationError('A tarefa ainda não possui rodada de validação vigente.')
        if criterion_id not in {item['criterion_id'] for item in self.CRITERIA}:
            raise ValidationError('Critério desconhecido.')
        decisions = dict(task['human_decisions'])
        previous = decisions.get(criterion_id)
        control_stream = f'task-control:{task_id}'
        control_events = self.events.read_stream(control_stream)
        latest = next((event for event in reversed(control_events)
                       if event['event_type'] == 'HUMAN_DECISION_RECORDED'
                       and event['payload']['criterion_id'] == criterion_id), None)
        latest_id = latest['payload']['decision_id'] if latest else None
        projected_id = previous['decision_id'] if previous else None
        if latest_id != projected_id:
            raise ConcurrencyConflict('A projeção da decisão humana está desatualizada.')
        expected_revision = control_events[-1]['stream_revision'] if control_events else 0
        decision_id = 'decision_' + uuid4().hex
        self._append_control(task_id, 'HUMAN_DECISION_RECORDED', f'decision:{decision_id}', {
            'decision_id': decision_id, 'validation_run_id': task['validation_run_id'],
            'criterion_id': criterion_id, 'action': action, 'actor_id': actor_id,
            'supersedes_decision_id': projected_id}, expected_revision=expected_revision)
        decisions[criterion_id] = {'decision_id': decision_id, 'action': action,
                                   'actor_id': actor_id}
        self._save_result(task_id, task['result_version_id'], task['assessment_id'],
                          task['validation_run_id'], task['run_generation'], task['manifest'],
                          task['validator_results'], decisions)
        return self.get(task_id)

    def _save_result(self, task_id, result_id, assessment_id, run_id, generation,
                     manifest, results, decisions):
        projections = []
        detail = {}
        for criterion in self.CRITERIA:
            cid = criterion['criterion_id']
            raw = results.get(cid, {})
            converted = {validator_id: ValidatorResult(validator_id, value['run_status'],
                         value.get('observation')) for validator_id, value in raw.items()}
            decision = decisions.get(cid, {}).get('action')
            projection = project_criterion(criterion['validators'], converted,
                                           EvidencePolicy(**criterion['evidence_policy']), decision)
            projections.append(projection)
            detail[cid] = {**projection.__dict__, 'description': criterion['description']}
        aggregate = project_assessment(projections)
        projection_json = {**aggregate.__dict__, 'criteria': detail}
        current = self.get(task_id)
        revision = int(current.get('assessment_revision') or 0) + 1
        self._append_control(task_id, 'ASSESSMENT_UPDATED',
                             f'assessment:{assessment_id}:revision:{revision}', {
            'assessment_id': assessment_id, 'assessment_revision': revision,
            'result_version_id': result_id, 'validation_run_id': run_id,
            'projection': projection_json})
        db = self.events.db()
        try:
            db.execute('''UPDATE validation_tasks SET result_version_id=?,assessment_id=?,
                validation_run_id=?,run_generation=?,assessment_revision=?,manifest=?,validator_results=?,
                human_decisions=?,projection=?,updated_at=? WHERE task_id=?''',
                (result_id, assessment_id, run_id, generation, revision,
                 json.dumps(manifest, sort_keys=True), json.dumps(results, sort_keys=True),
                 json.dumps(decisions, sort_keys=True), json.dumps(projection_json, sort_keys=True),
                 datetime.now(timezone.utc).isoformat(), task_id))
            db.commit()
        finally:
            db.close()

    def get(self, task_id: str) -> dict | None:
        """Retorna a projeção pública corrente de uma tarefa."""
        db = self.events.db()
        db.row_factory = sqlite3.Row
        try:
            row = db.execute('SELECT * FROM validation_tasks WHERE task_id=?',
                             (task_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            for field, default in (('manifest', None), ('validator_results', {}),
                                   ('human_decisions', {}), ('projection', None)):
                result[field] = json.loads(result[field]) if result[field] else default
            return result
        finally:
            db.close()
