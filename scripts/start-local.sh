#!/usr/bin/env bash
# Restore the already-installed SIH development services without recreating data.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ "${CONDA_DEFAULT_ENV:-}" != sih ]]; then
  echo 'Run conda activate sih first.' >&2
  exit 1
fi
mkdir -p var/logs
python - <<'PY'
from udaan.config import settings
s = settings()
if '/udaan_runtime?host=/tmp/udaan-pg-socket' not in s.database_url.get_secret_value() or (s.weaviate_url or '').rstrip('/') != 'http://127.0.0.1:8080':
    raise SystemExit('This helper restores the current local development setup only. For Compose, follow README.md.')
PY
pg_bin=/usr/lib/postgresql/18/bin
if ! "$pg_bin/pg_isready" -h /tmp/udaan-pg-socket -d udaan_runtime -q; then
  if [[ ! -f /tmp/udaan-test-pg/PG_VERSION ]]; then
    echo 'Existing PostgreSQL data is missing. Restore it or use the persistent Compose setup in README.md.' >&2
    exit 1
  fi
  mkdir -p /tmp/udaan-pg-socket
  python - <<'PY'
from udaan.runtime import clear_stale_local_postgres_runtime
clear_stale_local_postgres_runtime()
PY
  "$pg_bin/pg_ctl" -D /tmp/udaan-test-pg -l "$PWD/var/logs/postgres.log" \
    -o '-c listen_addresses= -k /tmp/udaan-pg-socket' start
fi
udaan migrate
if ! curl -fsS --max-time 2 http://127.0.0.1:8080/v1/.well-known/ready >/dev/null; then
  [[ -x var/bin/weaviate ]] || { echo 'Install the official Weaviate binary as described in README.md.' >&2; exit 1; }
  nohup env AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true \
    PERSISTENCE_DATA_PATH="$PWD/var/weaviate" DEFAULT_VECTORIZER_MODULE=none \
    ENABLE_API_BASED_MODULES=false CLUSTER_HOSTNAME=udaan-local \
    CLUSTER_GOSSIP_BIND_PORT=7100 CLUSTER_DATA_BIND_PORT=7101 RAFT_PORT=8300 \
    RAFT_INTERNAL_RPC_PORT=8301 AUTOSCHEMA_ENABLED=false DISABLE_TELEMETRY=true \
    GOMEMLIMIT=512MiB GOMAXPROCS=2 \
    var/bin/weaviate --host 127.0.0.1 --port 8080 --scheme http \
    >var/logs/weaviate.log 2>&1 </dev/null &
fi
if ! curl -fsS --max-time 2 http://127.0.0.1:6080/vnc.html >/dev/null; then
  nohup udaan desktop >var/logs/desktop-launch.log 2>&1 </dev/null &
fi
if ! curl -fsS --max-time 2 http://127.0.0.1:8000/api/v1/health >/dev/null; then
  nohup udaan api >var/logs/api.log 2>&1 </dev/null &
fi
if ! pgrep -u "$(id -u)" -f '[/]udaan worker' >/dev/null; then
  nohup udaan worker >var/logs/worker.log 2>&1 </dev/null &
fi
python - <<'PY'
import time
import httpx
for attempt in range(30):
    try:
        with httpx.Client(timeout=2) as client:
            for endpoint in ('http://127.0.0.1:8000/api/v1/health', 'http://127.0.0.1:6080/vnc.html', 'http://127.0.0.1:8080/v1/.well-known/ready'):
                client.get(endpoint).raise_for_status()
        break
    except httpx.HTTPError:
        time.sleep(1)
else:
    raise SystemExit('A local service did not start. Check var/logs/.')
print('Local services are reachable. Run udaan doctor for measured checks, then udaan.')
print('The host Qwen server is managed separately on macOS.')
PY
