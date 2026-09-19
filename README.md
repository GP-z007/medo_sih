# Udaan

A compact terminal console for **Add Source → Scrape → See Data**. The seven pages are Dashboard, Scrape, Sources, Data, AI, Logs and Settings. Browser jobs keep running when the console closes.

The predefined scope contains IndiGo, Air India, Air India Express, Akasa Air and SpiceJet. All five have deterministic contract-3 recipes and persisted real-fare evidence. IndiGo, Air India, Air India Express and Akasa have fresh successful validations; SpiceJet's latest run met an external HTTP 403 after two earlier six-row passes, so Udaan backed off and marked that source BLOCKED. See [SCRAPING_STATUS.md](SCRAPING_STATUS.md) and [SOURCE_MATRIX.md](SOURCE_MATRIX.md).

## Run in this workspace

```bash
cd /workspaces/medo_sih
conda activate sih
udaan ser
udaan
```

`udaan ser` reuses the installed local PostgreSQL/TimescaleDB data, AI Memory service, API, worker and visible desktop. It holds a startup lock, applies pending migrations, starts missing services, and waits for real health checks. Repeated execution was verified to retain one API, one worker/scheduler and one desktop. Recipe Tests reports OFFLINE because this outer container denies Docker mounts; AI candidates cannot be activated until isolation works. It does not create another environment, reset the database or start a model runtime. Logs are in `var/logs/`. Run `udaan doctor` for real dependency checks.

Eura Engine runs independently of the AI model. Dashboard shows Eura READY only after its library imports, extraction initializes and the recipe subsystem is accessible. Missing model credentials show AI NOT CONFIGURED. Normal five-airline collection uses saved deterministic Eura recipes without AI or memory lookups.

Forward VS Code ports **6080** and **8000** privately. Open `http://127.0.0.1:6080/vnc.html` to watch the browser. The existing VNC password is in `.desktop-secrets/password`; read that local file to connect. It is not tracked or logged.

The current database lives under `/tmp/udaan-test-pg`, with a Unix socket at `/tmp/udaan-pg-socket`. The runtime database `udaan_runtime` is separate from tests. Its contents survive service restarts, but **not deletion of that directory or container recreation**. Use the persistent Compose setup below before relying on long-term retention.

## Using the console

- **1 Dashboard:** actual service states, scrape counts, recent activity and measured resources. CPU/RAM describe the container; disk describes the filesystem. One CPU core equals 100%. Unknown limits remain unavailable.
- **2 Scrape:** choose one airline or **All Sources**, one booking window or **All Windows** (T+1/7/15/30/45), and one fare family or **All Available**. Set **Parallel Scrapes** from 1–5 (default 3), choose Normal, Careful or Strong Recovery, then run now or schedule the same durable group.
- **3 Sources:** manage the five predefined airlines and any user-added source. Edit, Turn On / Off, Build with Eura AI, Test, Archive and Recipe Details use the same generic source API. Test queues a real collection with the attached immutable recipe.
- **4 Data:** filter source, airline, airports, collection dates and booking time. Show Data applies database-side filtering. Previous/Next page through results. Export CSV/Parquet saves the current filtered page, including its offset; the dialog shows the finished file and download address. Advanced Filters contains recipe version, quality flag, column selection, sorting, grouping and aggregation. Additional allowlisted filters remain available through the API.
- **5 AI:** real model name, connectivity, last check, measured latency, last successful connection and AI Memory state. Test AI requests actual inference and a memory read/write/search/cleanup check. Technical details are behind Details.
- **6 Logs:** readable activity messages; select an event for details.
- **7 Settings:** default scraping mode and row count, browser view and service names. Edit saves preferences in `var/ui-preferences.json`. Connection details are under Advanced. Official Data API describes the future partner connection.

Each child scrape receives its own isolated browser context in one browser process. The worker enforces both the selected 1–5 group limit and the global limit. Group Details rolls up running, queued, waiting, succeeded, failed, blocked and skipped children. Same-airline windows remain isolated, including cookies and challenge state.

Normal tries an ordinary failed website collection up to 3 times with short waits, then stops and applies a source cooldown at a challenge. Careful tries up to 4 times with longer waits and verified human-assisted challenge handling. Strong Recovery tries up to 6 times with the longest bounded waits and repeated context verification. No mode retries through or solves challenges, injects tokens, rotates identity or bypasses a site's controls.

Scheduling presets include now, once, morning, afternoon, evening, daily, twice daily, three times daily, weekdays, weekends, weekly and custom. Times are stored with an IANA time zone, account for DST, show a plain-English summary, and enqueue through the same durable group path as Run Now.

Dropdowns, selected text and help controls are tested at 136×44 and 114×40 terminal cells; actual captures were also inspected at the 1366×900 desktop resolution. Action rows scroll horizontally when needed. Each page has a small [i] button; I opens its short help popup. Use number keys for pages, Tab/Shift+Tab to move between controls, Enter for selection/details, R to refresh, and Q to quit.

### Current Air India scope

Recipe `air_india/v2/manifest.yaml` uses the inspected public search controls, handles late cookie consent, and verifies the selected full date, route, cabin and passengers before extraction. Prices come from the requested cabin, not hidden duplicates or a different cabin. Nearby-airport results are excluded instead of being labelled as the requested airport.

The current recipe validates **one adult**, with displayed per-adult “From” fares. Other passenger combinations are rejected pending validation. Base fare, taxes and fees remain null where the page does not publish them. The displayed operating airline is retained where available; otherwise the trusted displayed flight-code mapping is recorded in extraction metadata. No fares are hardcoded.

To seed another configured runtime through the same generic API:

```bash
udaan import-source sources/air_india.json --recipe air_india/v2/manifest.yaml
```

In a fresh database, registration alone does not make a recipe selectable. Use `POST /api/v1/recipes/{id}/test` with a future SearchRequest to validate a registered manual recipe without attaching it first, then attach it only after a positive real collection. This endpoint rejects unfinished AI candidates; they must pass the isolated build pipeline. A verified empty result cannot establish recipe readiness.

The supplied seed reflects this SIH operator's permission. Re-import preserves existing source settings. Recipe versions are immutable; changed assets require a new version. Attaching a version does not change jobs already pinned to an older version.

### Build with Eura AI

Sources → Add Source → save name and public URL. Set collection permission under Advanced only when you have it, select the source, then **Build with Eura AI** and enter a test route/date. A source can be OFF and have no recipe while building.

The independent worker opens the website visibly and scans useful visible booking controls. Each element receives a temporary ID. The compact page state includes filtered roles, labels, placeholders, text and structural attributes, without input values, raw HTML, account data or challenge material. The configured AI model selects **one action on one supplied ID per request**; it never writes executable code or invents a selector. Udaan derives locators, validates the constrained action, executes through its trusted browser interface, verifies the actual effect, and observes again. The compiled recipe must pass isolation before activation. Failed actions get bounded replacement attempts.

Creation has separate search, repeated-result discovery, field mapping and real-fare validation phases. Only verified steps compile into Eura contract 2. The first compiled version is deliberately restricted to its actually validated route/date/cabin/passengers; replay rejects a different search rather than pretending fixed calendar clicks generalize. Existing Air India contract 1 remains supported. The trusted result validator checks context and actual card routes, prices and currencies. Available local flight times are validated without inventing UTC offsets. Persisted metrics count validated fares and mapped fields.

AI Memory stores bounded site/domain control discoveries, verified patterns, field mappings, successful recipe summaries and failure/fix knowledge. Retrieval uses the top three small BM25 matches before each planning stage. Stable content IDs prevent repeated identical writes. PostgreSQL remains authoritative for jobs, recipes and money. No raw page dump or fare-row stream goes into memory. Repair likewise chooses from observed structural IDs one field at a time.

`AI_RECIPE_TIMEOUT_SECONDS=90` controls recipe requests separately from the 30-second health timeout. Job history records request bytes, measured latency and JSON/schema validity, never raw prompts or server responses. Connectivity, timeout, malformed response, empty response, schema errors, oversized context and generation failure have distinct error codes. Normal Details shows readable status/history and Retry; technical records remain in **Advanced → Raw**.

Recipe Details lists only compatible registered recipes whose asset hash, contract, source identity and real validation/health checks pass. Rejections appear under **Advanced → Recipe compatibility**. No usable recipe produces **No working recipes → Build with Eura AI**. Attaching an incompatible, missing, changed or unvalidated asset returns its exact reason before changing the source pointer. Retry creates a linked job; API clients use `POST /api/v1/sources/{id}/build-recipe` with a SearchRequest and `GET /api/v1/sources/{id}/recipes` for compatibility.

**Current status (13 September 2026):** all five recipe implementations are integrated. Four sources have fresh successful validations. SpiceJet retains two prior successful six-row validations but is BLOCKED after the latest external HTTP 403. Runtime status may also become DEGRADED after an individual website failure; successful observations and immutable recipe evidence are retained. Docker isolation remains unavailable, so no AI-created candidate promotion is claimed.

### When the website needs help

Udaan pauses and shows **Udaan needs your help**. Complete the step manually in the same visible browser, then choose **Done — Check Again**. C confirms and requests validation; R requests validation without a confirmation; X cancels.

Confirmation alone never resumes a job. The owning worker verifies the current page and search identity. Failed checks keep the browser open and show the actual sanitized reason. The cumulative operator budget is 900 seconds per job; new prompts do not extend it. Rate-limit cooldowns cannot be overridden. Browser death or an expired worker lease terminates the interrupted attempt; Retry creates a linked new attempt.

Security challenges never trigger automatic solving, token injection, identity rotation, selector repair or AI prompts. Challenge screenshots, content, credentials and cookies are not persisted. Tests verify that the browser/context/page and cookies survive an operator wait and that recipe actions remain paused.

## Installation and separate service commands

Keep the existing Python 3.11 `sih` environment:

```bash
conda activate sih
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
bash .devcontainer/install-desktop.sh
udaan browser-install
udaan migrate
```

The current environment is Linux ARM64, PostgreSQL 18.6 and TimescaleDB 2.25.1. Browser installation downloads the upstream browser, not a model. The actual browser runs `headless=False` on display `:99`, with JavaScript, CSS and images enabled. Website-side asset failures may still occur.

When managing processes in separate terminals instead of the startup helper:

```bash
udaan desktop
udaan api
udaan worker
udaan
```

Each command stays running in its own terminal. The worker owns scheduling, collection, exports and repair. Restart the API/worker after changing configuration or implementation, once active jobs have finished or been cancelled. The helper starts missing services; it does not reload already-running processes.

### AI Memory

AI Memory stores useful page information and past fixes for Build/Repair. Normal scraping does not query it. READY requires measured read, write, search and cleanup checks. Dependency installation details are in [Advanced developer notes](docs/eura-advanced.md).

## Visible Udaan Browser

The actual headed window uses **Udaan Browser** as its product name. `udaan browser-install` and browser launch apply a small, idempotent patch to the installed browser's three branding string resources in `browser/omni.ja`. The original archive is retained as `browser/omni.upstream.ja`; engine code and license notices are unchanged. Reinstalling an upstream browser reapplies the patch on its next launch.

The desktop launcher serves a local noVNC copy from `var/desktop-web`, with a small **Udaan Browser** title and connection label. System noVNC files are untouched. Start it with `udaan ser`, then open `http://127.0.0.1:6080/vnc.html`. The private password remains in `.desktop-secrets/password`; never commit it. Restart the desktop only after active browser jobs finish.

Third-party attribution: Udaan Browser uses Camoufox internally, built on Mozilla Firefox, and noVNC for desktop access. Their original source/license notices remain in the installed browser resources and copied noVNC distribution, including noVNC's `LICENSE.txt`. Product branding does not replace that attribution.

## Configuration

Copy `.env.example` to your local `.env` and configure your own service endpoints. **Settings → AI** shows the effective provider name, base URL, model, masked key status and measured model state. This prototype uses environment configuration; restart the API and worker after changing it.

```dotenv
AI_PROVIDER=openai_compatible
AI_PROVIDER_NAME=NVIDIA
AI_BASE_URL=
AI_MODEL=
AI_API_KEY=
AI_TIMEOUT_SECONDS=30
AI_RECIPE_TIMEOUT_SECONDS=90
```

The provider is generic: any compatible endpoint can be configured. The label is optional. Base URL, model ID and API key are required; no key or model is assumed. Use the provider's API root (including `/v1` where required). Old `QWEN_*` settings are no longer used. Secrets stay in the ignored `.env` or process environment, never in recipes, responses or logs.

Startup, Dashboard and `udaan doctor` do not contact the model. **AI → Test AI** is explicit: it checks the configured model and a minimal response, reporting CHECKING, READY or FAILED. Configuration alone is NOT TESTED; incomplete configuration is NOT CONFIGURED. Eura remains usable independently of AI.

**Sources → Recipe** shows the current version, readiness, author, actual last test and repair times. Use **Test Recipe**, **Build with Eura AI**, or **Repair with Eura AI**. New sources show “No working recipe.” The build screen explains Open website → Read page → Ask AI → Test recipe → Save recipe, with START disabled until model credentials and Eura are available. Repair uses the captured failure and relevant AI Memory, then the existing isolated and live validation gates before promoting a new immutable version. Candidates never imply success before validation.

No real external AI build or repair has been tested in this phase. Waiting for the API base URL, API key and model ID.

The API binds to loopback. Set `API_TOKEN` before exposing it beyond loopback; the TUI uses it automatically. Keep secrets in `.env`, outside tracked files. The HTTP reference is at `http://127.0.0.1:8000/docs` for a local unauthenticated setup.

## Persistent Docker infrastructure

The provided Compose deployment uses persistent volumes and health checks. It has not been runtime-verified here. Docker CLI, dockerd and containerd are installed in this development container. A local daemon started successfully using the vfs driver, but even a minimal offline image build failed with `failed to mount ... operation not permitted`. The outer container lacks CAP_SYS_ADMIN and exposes read-only cgroups. Installing Docker inside it does not grant those kernel capabilities. The repair image and isolated live AI promotion remain unavailable.

`udaan ser` manages ordinary Udaan processes; it does not install or start a privileged Docker daemon. Automatic approval review rejected adding persistent privileged daemon startup. The safe minimal offline build was executed separately and encountered the mount denial above. An environment that supports isolated Docker containers is required for the following optional deployment commands.

Set a real private `POSTGRES_PASSWORD` and matching URL in `.env` (URL-encode special characters). From this development container, host-published services typically use:

```dotenv
DATABASE_URL=postgresql+psycopg://udaan:YOUR_LOCAL_PASSWORD@host.docker.internal:5432/udaan
WEAVIATE_URL=http://host.docker.internal:8080
```

```bash
docker compose up -d
docker compose ps
udaan migrate
docker build -f infra/repair.Dockerfile -t udaan-repair:local infra
```

For a shared container network, use the service DNS names; for local host processes, use the published loopback addresses. `udaan ser` verifies configured remote services; start those Compose services before invoking it. Migrations require TimescaleDB and fail explicitly if it is absent. The desktop uses the repository launcher/install script rather than the proposed desktop-lite feature.

## API and retained advanced functions

Versioned routes cover sources, recipes, schedules, jobs/history/operator actions, health, observations, queries, exports, weights, indices and repairs. Requests use validated types, allowlisted filters and parameterized queries. Arbitrary SQL is rejected. Preview defaults to 100 rows, capped at 1,000, with deterministic ordering. Exports run in the worker using a consistent database snapshot and bounded chunks. CSV protects spreadsheet formula prefixes; stored values remain unchanged.

Useful routes: `/api/v1/health`, `/sources`, `/jobs`, `/observations`, `/data/query`, `/data/export`, `/exports`, and `/exports/{id}/download` (all under `/api/v1`). Advanced clients can export up to the validated request limit independently of the simple UI's current-page export.

- **Rail A:** deterministic browser collection, operator waits, persistent schedules, validation, database queries and exports. Successfully verified with real Air India fares while AI was offline.
- **Rail B:** FUTURE / DEMONSTRATION — PRODUCTION PARTNERS: NONE. `/institutional/v1/schema`, `/fares`, `/batches`, `/submissions`, `/submissions/{id}` and `/health` accept only demonstration submissions. Provider/transaction identity and idempotency are validated. These records stay separate from authoritative observations and statistics.
- **Rail C:** constrained selector/parameter generation, static validation, isolated tests, contract validation, live validation, approval and immutable promotion. Every gate must pass. Security detectors, validators and destinations cannot be patched by the model. Offline containers are non-root, resource-limited, read-only and have no network, secrets or Docker socket. Live validation uses the trusted browser interface. Full repair remains unavailable until the Docker sandbox is available; it is not claimed as a successful live repair.

Approved quantity weights can be imported with `udaan import-weights /path/to/approved-weights.json`; the API exposes weight and index operations. The retained `udaan-monthly-matched-fisher-v1` methodology uses monthly matched single-adult route/cabin/currency/window strata: `L=Σ(p1*q0)/Σ(p0*q0)`, `P=Σ(p1*q1)/Σ(p0*q1)`, `F=sqrt(L*P)`, chained from 100. Inputs and provenance are persisted. Missing comparable approved weights/prices produce unavailable results. No weights, classifications or official compliance claims are invented. Index and repair controls are kept out of the simple main navigation.

## Project and verification

```text
udaan/{cli,runtime,api,tui,config}.py
udaan/{services,db,contracts,worker}.py
udaan/{browser,eura,planner,planner_contracts,challenges,health,resources,agent,data,index,desktop}.py
recipes/{air_india,indigo,akasa_air,star_air}/.../manifest.yaml
sources/air_india.json
migrations/versions/0001_*.py ... 0007_scrape_groups.py
infra/repair.Dockerfile, infra/validate_patch.py
.devcontainer/, docker-compose.yml
tests/, requirements.lock, pyproject.toml, .env.example
```

```bash
conda activate sih
UDAAN_TEST_DESKTOP=1 \
UDAAN_TEST_DATABASE_URL='postgresql+psycopg://vscode@/postgres?host=/tmp/udaan-pg-socket' \
pytest -q --tb=short
ruff check udaan tests
python -m pip check
alembic check
```

Tests create and remove isolated database schemas. Synthetic fares never enter the runtime database. The optional `pytest -m live` test requires `UDAAN_LIVE_SOURCE_ID` and `UDAAN_LIVE_SEARCH_JSON` containing a real future SearchRequest; it creates another real collection and never resolves challenges automatically. The normal suite skips that external test. Current verification results and remaining live limitations are recorded in VERIFICATION.md. This build separately exercised the real TUI-to-browser-to-database-to-export workflow; see the exact report in [VERIFICATION.md](VERIFICATION.md).
