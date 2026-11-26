"""Worker Linux independente da UI. Um job por vez; Docker obrigatório, sem fallback host."""
import argparse
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timezone

from jobs import JobStore, data_dir, now, MAX_OUTPUT_BYTES, MAX_OUTPUT_FILES

IMAGE = os.environ.get('HARNESS_RUNNER_IMAGE', 'qwen-harness-runner:1')
LOG_LIMIT = 4 * 1024 * 1024  # por fluxo; excedentes encerram o job


def container_name(row):
    """Deriva um nome Docker isolado para cada tentativa de uma tarefa."""
    return 'qh-' + row['id'].replace('_', '-') + '-' + row['active_token'][:12]


def extract_outputs(stream, destination):
    """Nunca extrai links, devices ou caminhos escolhidos para fora do destino."""
    destination.mkdir(parents=True, exist_ok=True)
    total = count = 0
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name:
                raise ValueError('Caminho inválido no artefato.')
            path = destination.joinpath(*name.parts)
            if not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Artefato fora do destino.')
            if not (member.isdir() or member.isfile()):
                raise ValueError('Artefatos não aceitam links ou arquivos especiais.')
            count += 1
            total += max(0, member.size)
            if count > MAX_OUTPUT_FILES or total > MAX_OUTPUT_BYTES:
                raise ValueError('Resultados excedem 1000 entradas ou 64 MiB.')
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, path.open('xb') as out:
                    shutil.copyfileobj(src, out, length=65536)
    return sorted(p for p in destination.rglob('*') if p.is_file())


class DockerEngine:
    """Encapsula as operações Docker necessárias para executar uma tarefa isolada."""

    def command(self, args, *, timeout=20, check=True):
        """Executa um comando Docker e converte falhas em exceções legíveis."""
        p = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout, shell=False)
        if check and p.returncode:
            raise RuntimeError(p.stderr.strip()[:2000] or 'Docker retornou erro.')
        return p

    def preflight(self):
        """Confirma a disponibilidade do daemon e da imagem de execução."""
        self.command(['info', '--format', '{{.ServerVersion}}'])
        self.command(['image', 'inspect', IMAGE])

    def create(self, row, snapshot):
        """Cria um contêiner restrito para o snapshot e os argumentos da tarefa."""
        request = json.loads(row['payload'])
        return self.command([
            'create', '--name', container_name(row), '--label', 'qwen-harness=true',
            '--network', 'none', '--read-only', '--user', '1000:1000', '--init',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
            '--cpus', '2', '--memory', '2g', '--memory-swap', '2g', '--pids-limit', '64',
            '--log-driver', 'none', '--ulimit', 'nofile=256:256',
            '--mount', f'type=bind,src={snapshot},dst=/source,readonly',
            '--tmpfs', '/work:rw,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=0700',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m,uid=1000,gid=1000,mode=0700',
            '--env', 'OMP_NUM_THREADS=2', '--env', 'OPENBLAS_NUM_THREADS=2',
            '--env', 'MKL_NUM_THREADS=2', IMAGE, str(request['timeout']),
            request['script'], *request['args']
        ])

    def start(self, row):
        """Inicia o contêiner anexando seus fluxos de saída ao worker."""
        return subprocess.Popen(['docker', 'start', '--attach', container_name(row)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def result(self, row):
        """Lê e valida o marcador de conclusão gravado dentro do contêiner."""
        # Python do contêiner lê no máximo 4096 bytes, mesmo se um script adulterar o marcador.
        p = self.command(['exec', container_name(row), 'python', '-I', '-c',
                          "from pathlib import Path; p=Path('/tmp/harness-result.json'); print(p.open().read(4096) if p.exists() else '')"],
                         timeout=10, check=False)
        if p.returncode or not p.stdout.strip():
            return None
        data = json.loads(p.stdout)
        code = data.get('exit_code')
        if code is not None and (not isinstance(code, int) or not -255 <= code <= 255):
            raise ValueError('Código de saída inválido.')
        return {'exit_code': code, 'timed_out': bool(data.get('timed_out', False))}

    def export(self, row, destination):
        """Copia e valida os artefatos produzidos no diretório de destino."""
        process = subprocess.Popen(['docker','cp',container_name(row) + ':/work/outputs/.','-'],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # Prazo também para streams tar incompletos ou Docker sem responder.
        timer = threading.Timer(30, process.kill)
        timer.start()
        try:
            files = extract_outputs(process.stdout, destination)
            if process.wait(timeout=5):
                raise RuntimeError('Docker não conseguiu coletar resultados.')
            return files
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    def remove(self, row):
        """Remove forçadamente o contêiner associado à tarefa."""
        result = self.command(['rm', '--force', container_name(row)], check=False)
        if result.returncode and 'No such container' not in result.stderr:
            raise RuntimeError(result.stderr.strip()[:2000] or 'Docker não removeu o contêiner.')


def collect_log(pipe, target, overflow):
    """Copia um fluxo de log até o limite e sinaliza qualquer excedente."""
    count = 0
    with target.open('wb') as output:
        while chunk := pipe.read(4096):
            room = max(0, LOG_LIMIT - count)
            output.write(chunk[:room])
            output.flush()
            count += len(chunk)
            if count > LOG_LIMIT:
                overflow.set()
    pipe.close()


def run_job(store, row, engine):
    """Executa uma tarefa reservada e publica seu estado, logs e artefatos."""
    token = row['active_token']
    root = store.base / row['session_id']
    jobdir = root / 'jobs' / row['id']
    attempt = jobdir / 'attempts' / token
    logs = attempt / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    destination = root / 'outputs' / 'jobs' / row['id'] / token
    temp = attempt / 'collected'
    process = None
    threads = []
    overflow = threading.Event()
    status, error, code, published = 'failed', None, None, []
    try:
        if store.get(root, row['id'])['cancel_requested']:
            status = 'cancelled'
        else:
            request = json.loads(row['payload'])
            security = request.get('security')
            if security:
                frozen_script = jobdir / 'snapshot' / request['script']
                if sha256(frozen_script.read_bytes()).hexdigest() != security['script_sha256']:
                    raise ValueError('Hash do script diverge da decisão de segurança.')
            engine.create(row, jobdir / 'snapshot')
            if not store.update_attempt(row['id'], token, status='running'):
                return
            process = engine.start(row)
            for name in ('stdout', 'stderr'):
                thread = threading.Thread(target=collect_log,
                    args=(getattr(process,name), logs / f'{name}.log', overflow), daemon=True)
                thread.start()
                threads.append(thread)
            start = time.monotonic()
            deadline = json.loads(row['payload'])['timeout'] + 10  # margem para inicializar o runner
            while True:
                store.heartbeat()
                if not store.token_is_active(row['id'], token):
                    # Outra tentativa tomou posse; o finally encerra somente este contêiner.
                    break
                if store.get(root, row['id'])['cancel_requested']:
                    status = 'cancelled'
                    break
                if overflow.is_set():
                    error = 'Limite de 4 MiB por fluxo de log excedido.'
                    break
                if time.monotonic() - start > deadline:
                    status = 'timed_out'
                    break
                result = engine.result(row)
                if result is not None:
                    code = result['exit_code']
                    status = 'timed_out' if result['timed_out'] else ('succeeded' if code == 0 else 'failed')
                    if status != 'succeeded':
                        error = 'Tempo limite excedido.' if result['timed_out'] else f'Python terminou com código {code}.'
                    files = engine.export(row, temp)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temp.rename(destination)
                    published = [str(destination / p.relative_to(temp)) for p in files]
                    published = [str(Path(p).relative_to(root)) for p in published]
                    break
                if process.poll() is not None:
                    error = 'Contêiner encerrou antes de entregar o resultado (verifique logs/RAM).'
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        status, error = 'interrupted', 'Worker interrompido pelo operador.'
        raise
    except Exception as exc:
        status, error = 'failed', str(exc)[:2000]
    finally:
        # rm -f encerra todo o contêiner, incluindo descendentes que escapem do grupo do script.
        try:
            engine.remove(row)
        except Exception as exc:
            status, error = 'interrupted', f'Não foi possível remover o contêiner: {exc}'[:2000]
        if process:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for thread in threads:
            thread.join(timeout=5)
        shutil.rmtree(temp, ignore_errors=True)
        if overflow.is_set() and status not in ('cancelled', 'interrupted'):
            status, error = 'failed', 'Limite de 4 MiB por fluxo de log excedido.'
        # Um cancelamento solicitado durante a coleta ainda precisa ser refletido no status.
        if store.token_is_active(row['id'], token) and store.get(root, row['id'])['cancel_requested'] and status != 'interrupted':
            status = 'cancelled'
        accepted = store.update_attempt(row['id'], token, status=status, exit_code=code, error=error,
                                        finished_at=now(), artifacts=json.dumps(published))
        if accepted:
            manifest_artifacts = []
            for relative in published:
                artifact = root / relative
                manifest_artifacts.append({
                    'uri': relative,
                    'digest': 'sha256:' + sha256(artifact.read_bytes()).hexdigest(),
                    'size_bytes': artifact.stat().st_size,
                })
            store.validation.record_job_result(row['id'], token, status, manifest_artifacts)
        if not accepted and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)


def recover(store, engine):
    """Mantém tentativas existentes; claims futuros retomam apenas leases vencidos."""
    return 0


def main():
    """Inicia um worker e consome tarefas da fila continuamente."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Processa no máximo um job e encerra.')
    args = parser.parse_args()
    store = JobStore()
    engine = DockerEngine()
    try:
        engine.preflight()
    except Exception as exc:
        raise SystemExit(f'Worker não iniciou: {exc}\nExecute bash preparar_executor.sh e confira docker info.')
    recover(store, engine)
    print('Worker ativo. Uma tarefa por vez neste processo; a fila aceita múltiplos workers.', flush=True)
    try:
        while True:
            store.heartbeat()
            row = store.claim()
            if row:
                print(f"Executando {row['id']} tentativa {row['attempt_number']}", flush=True)
                run_job(store, row, engine)
            if args.once:
                return
            if not row:
                time.sleep(1)
    except KeyboardInterrupt:
        print('Worker encerrado.', flush=True)


if __name__ == '__main__':
    main()
