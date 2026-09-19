"""Idempotent startup of the configured local prototype; AI checks are explicit."""
import asyncio
import fcntl
import os
import shutil
import subprocess
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psutil
from sqlalchemy import text

from udaan.config import settings
from udaan.db import engine
from udaan.health import HealthMonitor


def component_processes(component, root):
    matches = []
    for process in psutil.process_iter(['cmdline', 'uids', 'cwd']):
        try:
            args = process.info['cmdline'] or []
            if process.info['uids'].real != os.getuid():
                continue
            if any(Path(arg).name == 'udaan' and i + 1 < len(args) and args[i + 1] == component for i, arg in enumerate(args)) and process.info['cwd'] == str(root):
                matches.append(process)
            if component == 'memory' and any(Path(arg).name == 'weaviate' for arg in args) and process.info['cwd'] == str(root):
                matches.append(process)
        except (psutil.Error, TypeError):
            continue
    return matches


def running(component, root):
    return bool(component_processes(component, root))

def configuration_stale(process, root):
    try:
        env_file = root / '.env'
        return env_file.exists() and env_file.stat().st_mtime > process.create_time()
    except (OSError, psutil.Error):
        return False



def reachable(url, headers=None):
    try:
        return httpx.get(url, headers=headers, timeout=2).is_success
    except httpx.HTTPError:
        return False

def udaan_api_snapshot(url, headers=None):
    try:
        response = httpx.get(url + '/api/v1/health', headers=headers, timeout=2)
        payload = response.json()
        return payload if response.is_success and payload.get('api') == 'READY' else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def udaan_api_health(url, headers=None):
    return udaan_api_snapshot(url, headers) is not None

def listener_pids(port):
    result = set()
    try:
        for connection in psutil.net_connections(kind='tcp'):
            if connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port:
                result.add(connection.pid)
    except psutil.Error:
        return {None}
    return result

def terminate_owned(processes):
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=5)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass

def available_port(host, start):
    bind_host = '0.0.0.0' if host in {'0.0.0.0', '::'} else host
    for port in range(start + 1, min(start + 101, 65536)):
        family = socket.AF_INET6 if ':' in bind_host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((bind_host, port))
            except OSError:
                continue
            return port
    raise RuntimeError('No safe local API fallback port is available.')

def persist_api_address(root, cfg, port):
    """Atomically keep API, TUI, worker and later CLI processes on one address."""
    env_file = root / '.env'
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    client = urlsplit(cfg.api_url)
    hostname = client.hostname or '127.0.0.1'
    netloc = f'[{hostname}]:{port}' if ':' in hostname else f'{hostname}:{port}'
    values = {'API_URL': urlunsplit((client.scheme or 'http', netloc, '', '', '')),
              'API_HOST': cfg.api_host, 'API_PORT': str(port)}
    found = set()
    for index, line in enumerate(lines):
        key = line.split('=', 1)[0] if '=' in line and not line.lstrip().startswith('#') else None
        if key in values:
            lines[index], found = f'{key}={values[key]}', found | {key}
    lines.extend(f'{key}={value}' for key, value in values.items() if key not in found)
    temporary = root / '.env.udaan-address.tmp'
    temporary.write_text('\n'.join(lines) + '\n')
    if env_file.exists():
        os.chmod(temporary, env_file.stat().st_mode)
    os.replace(temporary, env_file)
    os.environ.update(values)
    settings.cache_clear()
    return settings()

def prepare_api(cfg, root, executable, headers, report):
    owned = component_processes('api', root)
    listeners = listener_pids(cfg.api_port)
    owned_pids = {process.pid for process in owned}
    healthy = udaan_api_health(cfg.api_url, headers)
    stale = any(configuration_stale(process, root) for process in owned)
    if healthy and listeners & owned_pids and not stale:
        report('API owner: healthy existing Udaan API')
        return cfg, True
    if healthy and not listeners & owned_pids:
        raise RuntimeError('API health answered but listener ownership is unknown; no process was changed.')
    if owned:
        report('API owner: Udaan API needs configuration reload' if stale else
               'API owner: stale Udaan API; terminating only Udaan-owned process')
        terminate_owned(owned)
        listeners = listener_pids(cfg.api_port)
    if listeners:
        if None in listeners:
            raise RuntimeError('API port owner is unknown; inspect socket permissions before changing it.')
        report('API owner: unrelated application; preserving it and selecting a Udaan fallback')
        cfg = persist_api_address(root, cfg, available_port(cfg.api_host, cfg.api_port))
    launch([executable, 'api'], 'api', root)
    return cfg, False


def database_ready():
    try:
        with engine().connect() as db:
            return db.scalar(text('SELECT 1')) == 1
    except Exception:
        return False


def clear_stale_local_postgres_runtime(data_dir=Path('/tmp/udaan-test-pg'),
                                       socket_dir=Path('/tmp/udaan-pg-socket')):
    """Remove only local runtime markers whose recorded postmaster is gone or unrelated."""
    pid_file = data_dir / 'postmaster.pid'
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().splitlines()[0])
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().replace(bytes([0]), b' ').decode(errors='replace')
    except (OSError, ValueError, IndexError):
        cmdline = ''
    if 'postgres' in cmdline and str(data_dir) in cmdline:
        return False
    pid_file.unlink(missing_ok=True)
    for name in ('.s.PGSQL.5432', '.s.PGSQL.5432.lock'):
        (socket_dir / name).unlink(missing_ok=True)
    return True


def launch(args, name, root, env=None):
    with (root / 'var/logs' / (name + '.log')).open('ab') as log:
        subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)


def start(report=print):
    cfg, root = settings(), Path.cwd().resolve()
    (root / 'var/logs').mkdir(parents=True, exist_ok=True)
    # Held across readiness waits, so concurrent invocations cannot duplicate processes.
    with (root / 'var/runtime-start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report('Udaan services — checking existing processes…')
        executable = shutil.which('udaan')
        if not executable:
            raise RuntimeError('Activate the existing sih environment first.')
        if not database_ready():
            if '/udaan_runtime?host=/tmp/udaan-pg-socket' in cfg.database_url.get_secret_value():
                pg = Path('/usr/lib/postgresql/18/bin/pg_ctl')
                if not pg.exists() or not Path('/tmp/udaan-test-pg/PG_VERSION').exists():
                    raise RuntimeError('Existing database or PostgreSQL tooling is missing. Restore it; no data was recreated.')
                Path('/tmp/udaan-pg-socket').mkdir(exist_ok=True)
                if clear_stale_local_postgres_runtime():
                    report('Removed stale local database runtime files; preserved all database data.')
                result = subprocess.run([str(pg), '-D', '/tmp/udaan-test-pg', '-l', str(root / 'var/logs/postgres.log'),
                    '-o', '-c listen_addresses= -k /tmp/udaan-pg-socket', 'start'], capture_output=True, timeout=45)
                if result.returncode:
                    raise RuntimeError('Database failed to start. See var/logs/postgres.log.')
            else:
                raise RuntimeError('Configured database is unreachable. Start its configured host or Compose service.')
        report('Database reachable; checking migrations…')
        result = subprocess.run([executable, 'migrate'], capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError('Database migration failed. Run udaan migrate for the categorized error.')
        memory_url = cfg.weaviate_url
        if memory_url and not reachable(memory_url + '/v1/.well-known/ready') and not running('memory', root):
            if urlsplit(memory_url).hostname in {'127.0.0.1', 'localhost'} and (root / 'var/bin/weaviate').exists():
                env = {**os.environ, 'AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED': 'true',
                    'PERSISTENCE_DATA_PATH': str(root / 'var/weaviate'), 'DEFAULT_VECTORIZER_MODULE': 'none',
                    'ENABLE_API_BASED_MODULES': 'false', 'CLUSTER_HOSTNAME': 'udaan-local',
                    'CLUSTER_GOSSIP_BIND_PORT': '7100', 'CLUSTER_DATA_BIND_PORT': '7101', 'RAFT_PORT': '8300',
                    'RAFT_INTERNAL_RPC_PORT': '8301', 'AUTOSCHEMA_ENABLED': 'false', 'DISABLE_TELEMETRY': 'true',
                    'GOMEMLIMIT': '512MiB', 'GOMAXPROCS': '2'}
                launch([str(root / 'var/bin/weaviate'), '--host', '127.0.0.1', '--port', str(urlsplit(memory_url).port or 8080), '--scheme', 'http'], 'weaviate', root, env)
        if not reachable(cfg.browser_view_url) and not running('desktop', root):
            missing = [x for x in ['Xvfb', 'x11vnc', 'websockify', 'openbox', 'xdpyinfo'] if not shutil.which(x)]
            if missing:
                raise RuntimeError('Browser View tooling missing. Run bash .devcontainer/install-desktop.sh.')
            launch([executable, 'desktop'], 'desktop-launch', root)
        headers = {'Authorization': 'Bearer ' + cfg.api_token.get_secret_value()} if cfg.api_token else {}
        cfg, api_existing = prepare_api(cfg, root, executable, headers, report)
        workers = component_processes('worker', root)
        if any(configuration_stale(process, root) for process in workers):
            report('Worker configuration changed; reloading the Udaan-owned worker')
            terminate_owned(workers)
            workers = []
        if not workers:
            launch([executable, 'worker'], 'worker', root)
        report('Waiting for services…')
        async def verify():
            monitor = HealthMonitor()
            deadline = time.monotonic() + 45
            while True:
                await monitor.check_dependencies()
                snapshot = monitor.snapshot()
                services = snapshot['services']
                if all(services.get(k, {}).get('status') == 'READY' for k in ['postgresql', 'timescaledb', 'worker', 'browser', 'weaviate']) or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(1)
            return monitor.snapshot()
        snapshot = asyncio.run(verify())
        services = snapshot['services']
        remote = udaan_api_snapshot(cfg.api_url, headers) or {}
        model = remote.get('model') or snapshot['model']
        states = {'Database': services.get('postgresql', {}), 'AI Memory': services.get('weaviate', {}),
                  'API': {'status': 'READY' if udaan_api_health(cfg.api_url, headers) else 'OFFLINE', 'existing': api_existing},
                  'Worker': services.get('worker', {}), 'Scheduler': services.get('worker', {}),
                  'Browser View': services.get('browser', {}),
                  'Recipe Tests': {'status': 'READY' if services.get('sandbox', {}).get('status') == 'AVAILABLE' else 'OFFLINE', 'reason': 'Trusted declarative candidate validation is unavailable.'},
                  'Eura': services.get('eura', {}),
                  'AI Model': {'status': model['connectivity'], 'reason': model.get('last_error')}}
        for name, value in states.items():
            ready = value.get('status') == 'READY'
            suffix = ' (existing)' if name == 'API' and value.get('existing') else ''
            report(f"{name:<15} {value.get('status', 'OFFLINE')}{suffix}")
            if not ready:
                report('  ' + (value.get('reason') or 'Not available; inspect var/logs/.'))
        core = all(states[k].get('status') == 'READY' for k in ['Database', 'API', 'Worker', 'Browser View'])
        report(('Udaan is ready. Run udaan.' if all(x.get('status') == 'READY' for x in states.values()) else 'Scraping is ready. Some AI functions are unavailable; see above. Run udaan.') if core else 'Udaan needs attention. See the failed service above.')
        return core, snapshot
