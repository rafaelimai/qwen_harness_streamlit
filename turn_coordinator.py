"""Executa turnos duráveis de continuação sem depender do navegador."""
import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from langchain_core.messages import SystemMessage, ToolMessage

from agente import build_agent, complete_pending_calls
from conversation_store import ConversationStore
from jobs import JobStore, data_dir
from storage import load_messages, save_messages, session_settings


def _settings(root):
    return session_settings(root, {
        'base_url': os.environ.get('LLAMA_BASE_URL', 'http://127.0.0.1:8080/v1'),
        'model': os.environ.get('LLAMA_MODEL', 'qwen'),
        'max_tokens': 4096,
        'temperature': 0.2,
    })


def _run_turn(root, turn_id, owner_id, generation, depth=0, event=None, root_turn_id=None):
    conversations = ConversationStore(root.parent)
    history = load_messages(root)
    transient = None
    inputs = list(history)
    if event is not None:
        transient = SystemMessage(
            id='transient_' + uuid4().hex,
            content=(
                'Evento confiável da harness JOB_RESULT_AVAILABLE. Não trate este evento como '
                'mensagem do usuário nem como autorização. Consulte get_job_status para o job '
                f"{event['job_id']}. Estado operacional: {event['execution_status']}; "
                f"validação: {event['validation_status']}. Retome o objetivo original. "
                'Você pode criar nova tarefa genuinamente necessária, sujeita aos mesmos gates '
                'de segurança; não repita a operação anterior por mero replay.'),
            additional_kwargs={'event_type': 'JOB_RESULT_AVAILABLE',
                               'result_version_id': event['result_version_id']})
        inputs.append(transient)
    settings = _settings(root)
    graph = build_agent(root, settings['base_url'], settings['model'],
                        float(settings['temperature']), int(settings['max_tokens']),
                        turn_id=turn_id, continuation_depth=depth,
                        root_turn_id=root_turn_id or turn_id)
    result = graph.invoke({'messages': inputs}, config={'recursion_limit': 24})
    messages = []
    for message in result['messages']:
        if transient is not None and message.id == transient.id:
            continue
        if message not in history:
            message.additional_kwargs['turn_id'] = turn_id
        messages.append(message)
    save_messages(root, messages, owner_id=owner_id, fencing_generation=generation)
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if payload.get('awaiting_user') or payload.get('awaiting_approval'):
                return 'WAITING_FOR_USER'
    return 'COMPLETED'


def run_once(base=None):
    """Reconcilia publicações e processa no máximo um turno por ciclo."""
    base = Path(base or data_dir()).resolve()
    jobs = JobStore(base)
    conversations = ConversationStore(base)
    jobs.reconcile_results()
    jobs.recover_abandoned_continuations()
    owner_id = 'coordinator_' + uuid4().hex

    # P-02: uma mensagem humana persistida antes do claim sempre tem prioridade.
    for pending in conversations.pending_human_turns():
        root = base / pending['session_id']
        if not root.is_dir():
            continue
        generation = conversations.claim_turn(
            pending['session_id'], pending['turn_id'], owner_id)
        if generation is None:
            continue
        error = None
        try:
            _run_turn(root, pending['turn_id'], owner_id, generation)
        except Exception as exc:
            error = str(exc)[:2000]
        conversations.finish_turn(
            pending['session_id'], pending['turn_id'], owner_id, generation, error)
        if error is None:
            jobs.resolve_waiting_for_user(pending['session_id'])
        return True

    for continuation in jobs.pending_continuations():
        if conversations.pending_human_turn(continuation['session_id']):
            continue
        root = base / continuation['session_id']
        turn_id = 'turn_' + continuation['continuation_id']
        conversations.register_turn(continuation['session_id'], turn_id, 'CONTINUATION')
        generation = conversations.claim_turn(
            continuation['session_id'], turn_id, owner_id)
        if generation is None:
            continue
        if not jobs.begin_continuation(continuation['continuation_id'], turn_id, generation):
            conversations.finish_turn(
                continuation['session_id'], turn_id, owner_id, generation,
                'Continuação deixou de estar pendente.')
            continue
        error = None
        retry = False
        try:
            outcome = 'COMPLETED'
            if not conversations.turn_has_assistant_output(continuation['session_id'], turn_id):
                outcome = _run_turn(root, turn_id, owner_id, generation,
                                    depth=int(continuation['depth']) + 1,
                                    event=continuation['event_payload'],
                                    root_turn_id=continuation['root_turn_id'])
            elif conversations.turn_waits_for_user(continuation['session_id'], turn_id):
                outcome = 'WAITING_FOR_USER'
            transition = (jobs.pause_continuation_for_user
                          if outcome == 'WAITING_FOR_USER' else jobs.complete_continuation)
            if not transition(continuation['continuation_id'], generation):
                raise RuntimeError('Fencing da continuação foi perdido antes do commit.')
        except Exception as exc:
            error = str(exc)[:2000]
            continuation_state = jobs.fail_continuation(
                continuation['continuation_id'], generation, error)
            retry = continuation_state == 'RETRY_PENDING'
        conversations.finish_turn(
            continuation['session_id'], turn_id, owner_id, generation, error, retry=retry)
        return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float,
                        default=float(os.environ.get('HARNESS_COORDINATOR_INTERVAL', '2')))
    args = parser.parse_args()
    if args.interval <= 0:
        raise SystemExit('O intervalo precisa ser positivo.')
    print('Coordenador de turnos ativo.', flush=True)
    while True:
        try:
            worked = run_once()
        except Exception as exc:
            print(f'Falha no ciclo do coordenador: {exc}', flush=True)
            worked = False
        if args.once:
            return
        time.sleep(0.1 if worked else args.interval)


if __name__ == '__main__':
    main()
