"""Testes do núcleo do contrato de validação 0.2."""
import threading

import pytest

from validation import (ConcurrencyConflict, EvidencePolicy, EventStore, HarnessValidation,
                        IdempotencyConflict, ValidationError, ValidatorResult,
                        project_assessment, project_criterion)


def result(identifier, status='COMPLETED', observation='PASS'):
    """Cria resultado conciso para os testes do reducer."""
    return ValidatorResult(identifier, status, observation)


def test_validator_operational_status_is_separate_from_observation():
    with pytest.raises(ValidationError):
        ValidatorResult('v1', 'TIMED_OUT', 'FAIL')
    with pytest.raises(ValidationError):
        ValidatorResult('v1', 'COMPLETED', None)
    timed_out = ValidatorResult('v1', 'TIMED_OUT')
    projection = project_criterion(['v1'], {'v1': timed_out})
    assert projection.verification_status == 'UNVERIFIED'
    assert projection.effective_status == 'PENDING'


def test_all_any_and_quorum_policies_are_deterministic():
    mixed = {'a': result('a', observation='PASS'), 'b': result('b', observation='FAIL')}
    all_projection = project_criterion(['a', 'b'], mixed, EvidencePolicy('ALL'))
    any_projection = project_criterion(['a', 'b'], mixed, EvidencePolicy('ANY'))
    quorum = project_criterion(['a', 'b'], mixed, EvidencePolicy('QUORUM', 1, 1))
    assert (all_projection.verification_status, all_projection.divergence) == ('REFUTED', True)
    assert (any_projection.verification_status, any_projection.divergence) == ('VERIFIED', True)
    assert quorum.verification_status == 'UNVERIFIED'
    empty = project_criterion([], {}, EvidencePolicy('ALL'))
    assert empty.verification_status == 'UNVERIFIED'


def test_manual_override_changes_acceptance_not_verification():
    failed = project_criterion(['v'], {'v': result('v', observation='FAIL')},
                               human_action='SET_ACCEPTED')
    assert failed.verification_status == 'REFUTED'
    assert failed.effective_status == 'ACCEPTED'
    assert failed.decision_effect == 'ACCEPT_OVERRIDE'
    assessment = project_assessment([failed])
    assert assessment.approved is True
    assert assessment.verification_status == 'REFUTED'
    assert assessment.approval_basis == 'HUMAN_ONLY'
    assert assessment.human_review_status == 'COMPLETE'


def test_approval_basis_is_causal_and_review_is_separate():
    confirmed = project_criterion(['a'], {'a': result('a')}, human_action='SET_ACCEPTED')
    automatic = project_criterion(['b'], {'b': result('b')})
    assessment = project_assessment([confirmed, automatic])
    assert assessment.approval_basis == 'AUTOMATED_ONLY'
    assert assessment.human_review_status == 'PARTIAL'


def test_assessment_requires_criteria_and_failure_precedes_partial():
    with pytest.raises(ValidationError):
        project_assessment([])
    passed = project_criterion(['a'], {'a': result('a')})
    pending = project_criterion(['b'], {'b': ValidatorResult('b', 'TIMED_OUT')})
    rejected = project_criterion(['c'], {'c': result('c', observation='FAIL')})
    partial = project_assessment([passed, pending])
    failed = project_assessment([passed, pending, rejected])
    assert (partial.acceptance_status, partial.verification_status) == ('PENDING', 'PARTIALLY_VERIFIED')
    assert (failed.acceptance_status, failed.verification_status) == ('REJECTED', 'REFUTED')


def test_event_store_idempotency_and_compare_and_swap(tmp_path):
    store = EventStore(tmp_path / 'validation.sqlite3')
    args = dict(event_type='CONTRACT_FROZEN', stream_id='task-control:t1',
                expected_revision=0, producer_type='SERVICE', producer_id='planner',
                idempotency_key='freeze-c1-v1', payload={'contract_id': 'c1'}, task_id='t1')
    first = store.append(**args)
    duplicate = store.append(**args)
    assert duplicate['event_id'] == first['event_id']
    assert first['stream_revision'] == 1 and first['global_position'] == 1
    with pytest.raises(IdempotencyConflict):
        store.append(**{**args, 'payload': {'contract_id': 'other'}})
    with pytest.raises(IdempotencyConflict):
        store.append(**{**args, 'schema_version': 2})
    with pytest.raises(ConcurrencyConflict):
        store.append(**{**args, 'idempotency_key': 'next', 'expected_revision': 0})


def test_event_store_has_one_concurrent_stream_winner(tmp_path):
    path = tmp_path / 'validation.sqlite3'
    EventStore(path)
    barrier = threading.Barrier(2)
    outcomes = []

    def compete(index):
        barrier.wait()
        try:
            event = EventStore(path).append(
                event_type='RESULT_SELECTED', stream_id='task-control:t1', expected_revision=0,
                producer_type='SERVICE', producer_id=f'orchestrator-{index}',
                idempotency_key='select', payload={'result_version_id': f'r{index}'}, task_id='t1')
            outcomes.append(event)
        except ConcurrencyConflict:
            outcomes.append(None)

    threads = [threading.Thread(target=compete, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sum(item is not None for item in outcomes) == 1
    assert len(EventStore(path).read_stream('task-control:t1')) == 1


def test_persisted_job_validation_lifecycle_and_human_acceptance(tmp_path):
    service = HarnessValidation(tmp_path / 'validation.sqlite3')
    contract = service.bootstrap_job('job-1', 'session-1')
    assert contract['frozen_event_id']
    service.record_attempt_started('job-1', 'attempt-1')
    evaluated = service.record_job_result('job-1', 'attempt-1', 'succeeded', [
        {'uri': 'outputs/result.txt', 'digest': 'sha256:abc', 'size_bytes': 3}])
    projection = evaluated['projection']
    assert projection['acceptance_status'] == 'PENDING'
    assert projection['verification_status'] == 'PARTIALLY_VERIFIED'
    assert projection['criteria']['execution_succeeded']['effective_status'] == 'ACCEPTED'
    assert projection['criteria']['result_accepted']['effective_status'] == 'PENDING'
    decided = service.decide('job-1', 'result_accepted', 'SET_ACCEPTED', 'user-1')
    assert decided['projection']['approved'] is True
    assert decided['projection']['approval_basis'] == 'MIXED'
    assert decided['projection']['human_review_status'] == 'PARTIAL'
    deferred = service.decide('job-1', 'result_accepted', 'DEFER_TO_EVIDENCE', 'user-1')
    assert deferred['projection']['approved'] is False
    assert deferred['projection']['acceptance_status'] == 'PENDING'


def test_failed_job_is_refuted_but_can_be_audited(tmp_path):
    service = HarnessValidation(tmp_path / 'validation.sqlite3')
    service.bootstrap_job('job-2', 'session-1')
    service.record_attempt_started('job-2', 'attempt-2')
    evaluated = service.record_job_result('job-2', 'attempt-2', 'failed', [])
    assert evaluated['projection']['acceptance_status'] == 'REJECTED'
    assert evaluated['projection']['verification_status'] == 'REFUTED'
    events = service.events.read_stream('task-control:job-2')
    assert [event['event_type'] for event in events] == [
        'CONTRACT_FROZEN', 'RESULT_SELECTED', 'ASSESSMENT_ACTIVATED',
        'VALIDATION_RUN_CREATED', 'VALIDATION_RUN_SELECTED', 'ASSESSMENT_UPDATED']


def test_recording_same_result_is_idempotent(tmp_path):
    service = HarnessValidation(tmp_path / 'validation.sqlite3')
    service.bootstrap_job('job-3', 'session-1')
    service.record_attempt_started('job-3', 'attempt-3')
    first = service.record_job_result('job-3', 'attempt-3', 'succeeded', [])
    second = service.record_job_result('job-3', 'attempt-3', 'succeeded', [])
    assert second['result_version_id'] == first['result_version_id']
    assert second['assessment_revision'] == first['assessment_revision'] == 1
