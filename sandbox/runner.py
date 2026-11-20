"""Executado somente no contêiner. Mantém tmpfs vivo até a coleta do worker."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def execute(script, args, timeout, source=Path('/source'), work=Path('/work'), result=Path('/tmp/harness-result.json')):
    """Executa um script no workspace efêmero e grava seu resultado atomicamente."""
    for folder in ('inputs','work'):
        shutil.copytree(source / folder, work / folder, dirs_exist_ok=True)
    (work / 'outputs').mkdir(exist_ok=True)
    os.chdir(work)
    child = subprocess.Popen([sys.executable, '-u', script, *args], start_new_session=True,
                             stdin=subprocess.DEVNULL)
    timed_out = False
    try:
        code = child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        code = None
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
    # Escrita atômica: o worker nunca lê JSON pela metade.
    temp = result.with_suffix('.tmp')
    temp.write_text(json.dumps({'exit_code': code, 'timed_out': timed_out}))
    temp.replace(result)


if __name__ == '__main__':
    try:
        execute(sys.argv[2], sys.argv[3:], int(sys.argv[1]))
    except Exception as exc:
        print(f'Falha no runner: {exc}', file=sys.stderr)
        raise
    # O tmpfs desaparece ao parar o contêiner: coletar outputs antes de removê-lo.
    # Se o worker morrer, este prazo impede um contêiner ocioso indefinido.
    time.sleep(180)
