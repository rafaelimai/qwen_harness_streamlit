"""Teste opcional REAL de Docker. Ative HARNESS_DOCKER_TESTS=1 no homelab."""
import os
from pathlib import Path
import tempfile
import threading
import time

import pytest
from jobs import JobStore
from storage import create_session, write_text_file
from worker import DockerEngine, run_job

pytestmark = pytest.mark.skipif(os.environ.get('HARNESS_DOCKER_TESTS') != '1',
                                reason='Docker real: ative HARNESS_DOCKER_TESTS=1')


def test_container_success_isolation_failure_timeout_and_cancel():
    """Exercita no Docker real sucesso, isolamento, falha, timeout e cancelamento."""
    engine = DockerEngine()
    engine.preflight()
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = create_session(base)
        store = JobStore(base)
        code = '''from pathlib import Path
import socket
print('Executor funcionando')
try:
    Path('/source/work/check.py').write_text('alterado')
    raise AssertionError('Snapshot ficou gravável')
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
    raise AssertionError('Rede habilitada')
except OSError:
    pass
Path('outputs/resultado.csv').write_text('x,y\\n1,2\\n')
'''
        write_text_file(root, 'work/check.py', code)
        job = store.enqueue(root, 'work/check.py', [], 30, 'success')
        run_job(store, store.claim(), engine)
        result = store.get(root, job['job_id'])
        assert result['status'] == 'succeeded', result
        assert (root / result['artifacts'][0]).read_text().startswith('x,y')
        assert (root / 'work/check.py').read_text() == code
        write_text_file(root, 'work/fail.py', "raise RuntimeError('falha intencional')")
        job = store.enqueue(root, 'work/fail.py', [], 10, 'failure')
        run_job(store, store.claim(), engine)
        assert store.get(root, job['job_id'])['status'] == 'failed'
        write_text_file(root, 'work/sleep.py', 'import time; time.sleep(60)')
        job = store.enqueue(root, 'work/sleep.py', [], 1, 'timeout')
        run_job(store, store.claim(), engine)
        assert store.get(root, job['job_id'])['status'] == 'timed_out'
        job = store.enqueue(root, 'work/sleep.py', [], 60, 'cancel')
        thread = threading.Thread(target=run_job, args=(store, store.claim(), engine))
        thread.start()
        try:
            deadline = time.monotonic() + 20
            while store.get(root, job['job_id'])['status'] == 'starting' and time.monotonic() < deadline:
                time.sleep(0.2)
            store.cancel(root, job['job_id'])
        finally:
            thread.join(timeout=40)
        assert not thread.is_alive()
        assert store.get(root, job['job_id'])['status'] == 'cancelled'
