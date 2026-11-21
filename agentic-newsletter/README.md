# Agentic AI Newsletter Automation (Docker-first)

Collects articles from allowlisted RSS feeds, lets subscribers pick topics, runs a multi-step **agent pipeline**
(curate → summarise with citations → verify claims → assess risk → decide auto-send / human review / skip), and a
**scheduler worker** delivers **one personalised email per subscriber**. Everything runs in Docker. Demo mode is fully
offline (fixture feeds + mock LLM + Mailpit) and needs no accounts.

> **Read this first: what this build contains.** This ZIP implements **Phase 1 completely, part of Phase 2 and the web interface**
> (Verifier, risk matrix, review queue, SMTP with allowlist, `check-smtp`, `send-test`, `check-feeds`, live feed mode,
> **Streamlit web UI** in its own container).
> It does **not** contain Groq/Ollama providers, model validation against a provider, free-text
> preferences → plan, IMAP ingestion or the `ollama` compose profile. See *Known limitations*.
> **Docker itself could not be run where this was built** — see *Verification*.

## Quick start (from the project root, one command per block)

```bash
./run.sh doctor
```
```bash
./run.sh init-env
```
```bash
./run.sh up
```
```bash
./run.sh smoke
```

`smoke` ends with `RESULT: PASS` or `RESULT: FAIL` plus reasons. Open Mailpit (default http://localhost:8026) to see the demo emails.
**Web interface: http://localhost:8502** (printed by `./run.sh up`). API docs: http://localhost:8100/docs (ports come from `.env`).

## Web interface

A Streamlit app in its own container (`ui/`, service `ui`). It talks **only to the API** (the API stays the single source of truth); it has no database
access and does not receive `.env`, so it never sees your passwords. It has **no login**, so it is published on `127.0.0.1` only
(reachable from this computer, not from your network).

| Page | What you can do |
|---|---|
| Overview | Worker/email/article/AI status, key numbers, runs-per-day chart, decision chart, average relevance per edition, next planned deliveries, recent runs, fetch articles now |
| Subscribers | Add, edit (name, language, topics, active), delivery preferences (max articles, look-back, style, excluded keywords, sources), deactivate; topic popularity chart |
| Schedules | Create/edit (time, timezone, days, topics, sources), enable/disable, **Run now**, run history, next delivery time |
| Review queue | Read each held draft, **Approve and send** or **Reject** (reason required) |
| History | Editions with per-recipient status/relevance/risk/errors, **Retry failed emails**; all runs with filters |
| Sources | Source list, topics, fetch articles now with counts and per-source errors |
| System | Health, redacted configuration, SMTP connection test |

In real email mode (`EMAIL_MODE=smtp`) the UI shows a warning and requires a tick-box before **Run now** or **Approve**.

Optional live steps (they use the network from *your* machine):

```bash
./run.sh check-feeds
```
```bash
./run.sh check-llm
```
```bash
./run.sh check-smtp
```
```bash
./run.sh send-test your-own-address@example.com
```

`check-smtp` and `send-test` need `EMAIL_MODE=smtp`; `send-test` refuses unless your address is in `REAL_SEND_ALLOWLIST`, no other
active subscriber exists and no schedule is enabled. `check-llm` only reports the mock provider in this build.

## Commands

| Command | What it does |
|---|---|
| `./run.sh doctor` | OS, architecture, Docker/Compose, docker access without sudo, disk, timezone, **port conflicts**, `.env` state (secrets redacted) |
| `./run.sh init-env` | Creates `.env` (mode 600): random DB password + signing key, detected timezone, free ports, optional **hidden** SMTP prompts |
| `./run.sh up` / `./run.sh down` / `./run.sh status` | Start (build, migrate, wait for health, prints the web UI address) / stop / list. `up` adds `HOST_PORT_UI` to an older `.env` automatically |
| `./run.sh logs [service]` | Recent logs (`./run.sh logs worker -f` follows) |
| `./run.sh psql` | psql inside the project database |
| `./run.sh reset` | Deletes this project's containers and volume after you type `yes` |
| `./run.sh test` | pytest in a container against an ephemeral Postgres database |
| `./run.sh test-ui` | Web interface tests in a container (stub API, no database) |
| `./run.sh smoke` | End-to-end check against the real stack |
| `./run.sh check-llm` / `check-smtp` / `check-feeds` / `send-test <address>` | Optional live checks |

Containers/volumes/network are prefixed `agentic_nl_`. Default host ports: Postgres 55433, Mailpit SMTP 1026, Mailpit UI 8026, API 8100, web UI 8502 (127.0.0.1 only)
(your old project's 55432/1025/8025/8000/8010/8011/8501 are untouched).

## Configuration 

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./local.db` | Database URL. Set automatically for containers; never edit by hand. |
| `TIMEZONE` | `UTC` | IANA timezone used as the default for new schedules (detected by init-env). |
| `SECRET_KEY` | (empty) | Signs unsubscribe links (generated by init-env). Changing it invalidates old links. |
| `PUBLIC_BASE_URL` | `http://localhost:8100` | Base URL used in unsubscribe links inside emails. |
| `DEFAULT_LANGUAGE` | `en` | `en` or `de`. |
| `FEED_MODE` | `fixture` | `fixture` (offline demo feeds) or `live` (real RSS). |
| `FEED_FRESHNESS_DAYS` | `30` | Articles older than this are dropped at ingestion. |
| `INGEST_MIN_INTERVAL_MINUTES` | `30` | A run re-ingests feeds only if the last ingest is older than this. |
| `SOURCES_FILE` | `sources.yaml` | Allowlisted feeds file. |
| `LLM_PROVIDER` | `mock` | Only `mock` exists in this build; anything else is refused at startup. |
| `LLM_MODEL` | (empty) | Reserved for Phase 2 (no placeholder default on purpose). |
| `EMAIL_MODE` | `mailpit` | `mailpit` (demo, default) or `smtp` (real mail). |
| `MAILPIT_SMTP_HOST` | `mailpit` | Mailpit host inside the Docker network. |
| `MAILPIT_SMTP_PORT` | `1025` | Mailpit SMTP port inside the Docker network. |
| `MAILPIT_API_URL` | `http://mailpit:8025` | Mailpit HTTP API (used by smoke). |
| `SMTP_HOST` | (empty) | Real SMTP server (EMAIL_MODE=smtp). |
| `SMTP_PORT` | `587` | Real SMTP port. |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none`. Certificates are always verified. |
| `SMTP_USERNAME` | (empty) | Login name (often differs from the sender). |
| `SMTP_PASSWORD` | (empty) | Login password / app password (set via hidden prompt). |
| `SMTP_SENDER` | (empty) | From: address (separate from the login). |
| `SMTP_TIMEOUT_SECONDS` | `20` | Socket timeout for SMTP. |
| `REAL_SEND_ALLOWLIST` | (empty) | Comma list of addresses/domains allowed to receive REAL mail. Empty = nobody. |
| `EMAIL_MAX_PER_MINUTE` | `0` | Rate limit for sending, 0 = unlimited. |
| `MAIL_FROM_DEMO` | `Agentic Newsletter <newsletter@agentic-nl.local>` | From: header in demo mode. |
| `SCHEDULE_SCAN_SECONDS` | `30` | How often the worker looks for due schedules. |
| `SCHEDULE_CATCHUP_MINUTES` | `15` | A delivery is still due this many minutes after its scheduled minute. |
| `CURATOR_MIN_SCORE` | `0.4` | Minimum per-article relevance to be considered. |
| `RELEVANCE_MIN` | `0.6` | Mean relevance below this = skip (before any LLM call). |
| `RELEVANCE_HIGH` | `0.8` | Mean relevance for the high-confidence auto-send rule (>= 2 articles). |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `HOST_PORT_API` | (generated) | Host port for the API (default 8100). |
| `HOST_PORT_DB` | (generated) | Host port for Postgres (default 55433). |
| `HOST_PORT_MAILPIT_SMTP` | (generated) | Host port for Mailpit SMTP (default 1026). |
| `HOST_PORT_MAILPIT_UI` | (generated) | Host port for Mailpit UI (default 8026). |
| `HOST_PORT_UI` | (generated) | Host port for the web interface (default 8502, bound to 127.0.0.1). |
| `POSTGRES_DB` | (generated) | Compose only: database name. |
| `POSTGRES_PASSWORD` | (generated) | Compose only: generated by init-env. |
| `POSTGRES_USER` | (generated) | Compose only: database user. |

## API endpoints (a test checks each one exists)

| Endpoint | Purpose |
|---|---|
| `GET /api/diagnostics` | diagnostics |
| `POST /api/diagnostics/smtp-check` | smtp check |
| `GET /api/editions` | list editions |
| `GET /api/editions/{newsletter_id}` | get edition |
| `POST /api/editions/{newsletter_id}/retry` | retry edition |
| `POST /api/ingest` | trigger ingest |
| `GET /api/review` | review queue |
| `POST /api/review/{draft_id}/approve` | approve |
| `POST /api/review/{draft_id}/reject` | reject |
| `GET /api/runs` | all runs |
| `GET /api/schedules` | list schedules |
| `POST /api/schedules` | create schedule |
| `DELETE /api/schedules/{schedule_id}` | Disables the schedule (run history is kept). |
| `PATCH /api/schedules/{schedule_id}` | patch schedule |
| `POST /api/schedules/{schedule_id}/run-now` | run now |
| `GET /api/schedules/{schedule_id}/runs` | schedule runs |
| `GET /api/sources` | list sources |
| `GET /api/subscribers` | list subscribers |
| `POST /api/subscribers` | create subscriber |
| `DELETE /api/subscribers/{subscriber_id}` | Soft delete: deactivates the subscriber (history stays intact). |
| `GET /api/subscribers/{subscriber_id}` | get subscriber |
| `PATCH /api/subscribers/{subscriber_id}` | patch subscriber |
| `GET /api/subscribers/{subscriber_id}/preferences` | get preferences |
| `PUT /api/subscribers/{subscriber_id}/preferences` | put preferences |
| `GET /api/topics` | list topics |
| `GET /health` | health |
| `GET /unsubscribe/{token}` | unsubscribe page |
| `POST /unsubscribe/{token}` | unsubscribe confirm |

## How it works

- **Pipeline** (`app/pipeline.py`, explicit state machine, one audit row per node in `agent_runs`): curator → summariser (JSON, one repair retry) →
  verifier (each claim vs its cited source; unsupported/contradicted claims removed and recorded in `newsletter_claims`) → risk reviewer →
  decision → personaliser. Relevance below `RELEVANCE_MIN` skips **before** any LLM call.
- **Decision matrix:** low risk + relevance ≥ 0.8 + ≥ 2 articles → send; low risk + ≥ 0.6 + ≥ 1 → send; medium → review; high/critical → review; relevance < 0.6 → skip.
- **Worker:** scans every `SCHEDULE_SCAN_SECONDS`. A delivery is due from its scheduled minute until `SCHEDULE_CATCHUP_MINUTES` later (crosses midnight and DST gaps).
  The run key comes from the *scheduled moment*; the unique `schedule_runs` row is **inserted and committed before any work**. A run that dies mid-send stays visibly `running`.
  Each recipient's pipeline runs in a savepoint; failures are stored with the stage (`connecting`, `STARTTLS`, `login`, `send`, …); one bad schedule never stops the others.
- **Statuses:** `sent`, `partial_failure`, `pending_review`, `skipped_low_relevance`, `skipped_no_new_articles`, `failed` — derived from what the mail server actually accepted.
- **Email:** one message per subscriber, text + HTML, `Date`, `Message-ID`, `List-Unsubscribe`, `List-Unsubscribe-Post`, signed unsubscribe link. TLS is always verified; no silent downgrade;
  exactly one login attempt (PLAIN, else LOGIN) and the server's real reply code/text is reported; credentials are never sent unencrypted; real mode needs `REAL_SEND_ALLOWLIST`.
- **Retry:** failed *sends* can be retried (`POST /api/editions/{newsletter_id}/retry`) without contacting recipients who already received it.
- **Migrations:** Alembic (`migrations/`), run automatically by the `migrate` service before API and worker.

## Assumptions (all changeable)

| Decision | Choice |
|---|---|
| Default host ports | Postgres 55433, Mailpit 1026/8026, API 8100, web UI 8502 (init-env moves to the next free port if busy) |
| Web UI access | no login, therefore bound to 127.0.0.1 only; never put it behind a public port without authentication |
| Web UI times | shown in the `TIMEZONE` from `.env`; schedule rows show their own timezone |
| Timezone | Detected from the host by `init-env`; schedules store their own IANA zone |
| Catch-up window / scan interval | 15 min / 20 s |
| Freshness window | 30 days |
| Lookback / max articles per subscriber | 168 h / 5 |
| Relevance | mock scorer: title hit ≥ 0.8, 2+ body hits 0.6, 1 body hit 0.4; newsletter relevance = mean of selected articles |
| Weekdays | integers, Monday = 0; empty list is rejected |
| Schedule `topics` empty | all topics; a subscriber is mailed only for topics they follow |
| DELETE endpoints | soft delete (subscriber deactivated / schedule disabled) so history stays intact |
| Unsubscribe | GET shows a confirm button, POST performs it (also the RFC 8058 one-click target) |
| One SMTP connection per recipient | simple and robust at this scale |
| Typed models | plain dataclasses + a hand-written validator in the core; pydantic only in the API layer |
| Mailpit health | healthcheck uses `/mailpit readyz`; dependants wait for `service_started`, and `up` waits on the API instead |
| Python deps | top-level versions pinned in `requirements.txt` (no hash lock file) |
| Tests live in the app image | so `./run.sh test` needs no bind mount |

## Known limitations

- **Not in this build:** Groq/Ollama providers, model validation and a real `check-llm`, free-text preference → plan, rate-limited provider handling beyond `EMAIL_MAX_PER_MINUTE`, IMAP ingestion, `ollama` profile.
- The mock "summariser" quotes the first sentences of each article and does **not translate**: a German subscriber gets German headings but English articles stay English (a real LLM would translate).
- Prompt-injection: the prompt framing (delimiters, guard text, delimiter forging neutralised) is tested; resistance of a **real** model is untested.
- Only send-stage failures can be retried; a generation failure needs a new run. A run that crashed stays `running` until you look at it (deliberate, visible).
- Feed fetch validates the resolved address before connecting; a DNS-rebinding race between check and connect is not fully excluded.
- `requirements.txt` has no hash lock; the app image also contains pytest/tests.
- SQLite is only used by the test suite; production is PostgreSQL.
- Web UI: no authentication (local-only by design); no article browser and no per-source feed health (use `./run.sh check-feeds`); "next delivery" is approximate around daylight-saving changes (the worker is authoritative); long actions such as **Run now** time out in the browser call after 5 minutes (the run itself continues, check History).

## Troubleshooting (keyed by the exact message)

| Message | Meaning / next step |
|---|---|
| `ERROR: .env not found in ... Run: ./run.sh init-env` | Run `./run.sh init-env` from the project root. |
| `ERROR: .env already exists. Keep it, or run ./run.sh init-env --force` | Keep it, or `./run.sh reset` then `./run.sh init-env --force`. |
| `ERROR: docker not found.` | Install Docker, then `./run.sh doctor`. |
| `ERROR: cannot talk to the Docker daemon without sudo.` | Add your user to the `docker` group (log out/in) or start the daemon; paste `./run.sh doctor`. |
| `CONFLICT: host port N (HOST_PORT_X) is in use by container: ...` / `ERROR: a host port is taken` | Edit that `HOST_PORT_*` in `.env`, or stop the other container. |
| `migration container failed (exit N):` | The log tail is printed; usually the DB password in `.env` no longer matches the volume → `./run.sh reset`. |
| `API did not become healthy in time.` | `./run.sh logs api migrate`. |
| `RESULT: FAIL` + `worker picked up the due schedule` | The worker did not run the schedule: `./run.sh logs worker`. |
| `smoke needs EMAIL_MODE=mailpit` | Set `EMAIL_MODE=mailpit` in `.env` for the demo. |
| `smoke needs SCHEDULE_CATCHUP_MINUTES >= 5` | The smoke schedule is 3–4 minutes in the past. |
| `[connecting] cannot connect to HOST:PORT` | Wrong host/port, server down, or firewall. |
| `[greeting] ...` | Reached something that is not an SMTP server (wrong port). |
| `[TLS handshake] ...` / `[STARTTLS] server certificate not trusted` | Certificate not trusted or `SMTP_SECURITY` does not match the port (587 = starttls, 465 = ssl). |
| `[STARTTLS] server does not offer STARTTLS - refusing to continue without encryption` | Use `ssl` on 465 or another server; the system never downgrades. |
| `[login] ... (server code 535)` | Rejected credentials; many providers need an app password. |
| `[login] server closed the connection while logging in (no reply)` | The server hung up after the login attempt — usually rejected credentials. |
| `[login] refusing to send credentials over an unencrypted connection` | `SMTP_SECURITY=none` with a username is refused. |
| `[allowlist] recipient X is not in REAL_SEND_ALLOWLIST` | Add the address/domain to `REAL_SEND_ALLOWLIST` if you really want to send. |
| `[send] recipient refused: ...` | The server rejected the recipient/sender/content. |
| `EMAIL_MODE=smtp needs SMTP_HOST, SMTP_SENDER to be set` | Fill those in `.env`. |
| `SECRET_KEY is empty - run ./run.sh init-env` | `.env` has no signing key. |
| `EMAIL_MODE='real' is invalid. Allowed values: mailpit, smtp` | The mode is called `smtp`. |
| `LLM_PROVIDER='groq' is not available in this build` | Only `mock` exists here. |
| `schedule is malformed: unknown timezone ...` | Fix the schedule's IANA timezone (`PATCH /api/schedules/{schedule_id}`). |
| `REFUSED: N other active subscriber(s) exist` / `enabled schedule(s) exist` | `send-test` must not be able to reach anyone else. |
| `cannot connect to the database: ...` | DB not up yet or wrong URL: `./run.sh status`. |
| `WARNING: the web interface is not answering yet` | `./run.sh logs ui`; the first start can take longer. |
| UI: `cannot reach the API at http://api:8000` | The API container is down: `./run.sh status`, then `./run.sh logs api`. |
| UI: `The worker has not reported recently` | Scheduled newsletters will not go out: `./run.sh logs worker`. |
| UI page does not open | Use the exact address printed by `./run.sh up` (`HOST_PORT_UI` in `.env`); it only listens on this computer. |

## Verification (what was actually run where this was built)

Build sandbox: Ubuntu 24.04, Python 3.12.3, **no network, no Docker, no PostgreSQL, no Mailpit, no pytest/FastAPI/Alembic/psycopg**.
Tests are `unittest` classes (pytest runs them unchanged) so they could run there.

| Claim | How verified | Status |
|---|---|---|
| 175 tests across 9 files pass (169 from the earlier build + 6 web-UI contract tests) | `python3 -m unittest discover -s tests` on SQLite + real local SMTP servers (STARTTLS, implicit TLS, AUTH, hang-ups) | **Verified** |
| Whole smoke scenario (due schedule → real worker process → per-recipient mail → no duplicates → stage-specific failure) | `tests/test_native_smoke.py` runs `app.smoke` against a real `python -m app.worker` subprocess, a real SMTP server and a fake Mailpit HTTP API; worker also checked for graceful SIGTERM | **Verified** (stand-ins, see below) |
| 20 of 20 deliberately broken fixes are caught by the tests | mutation script: shared `To:`, no savepoint, no catch-up, status always `sent`, no claim-before-send, unpersisted failures, no schedule isolation, allowlist bypass, TLS downgrade/verification off, login retry, hidden stage, doc/endpoint drift, … (two mutants first survived; tests were added) | **Verified** |
| `init-env`, `doctor`, `.env` mode 600, secrets never printed, unknown/duplicate command handling | executed in a scratch copy | **Verified** (without Docker present) |
| `run.sh` syntax | `bash -n` | **Verified** |
| PostgreSQL behaviour (placeholder translation, `ON CONFLICT`, savepoints, `psycopg` rows) | tests are written to run on Postgres via `TEST_DATABASE_ADMIN_URL`, SQL is ANSI-conservative | **NOT verified** — no Postgres here. `./run.sh test` is the first real check. |
| Alembic migration | `0001_initial.py` executes `migrations/sql/0001_initial.sql` (same file the tests apply to SQLite) | **NOT verified** — Alembic not installable here |
| FastAPI app | `py_compile` + AST check that README endpoints == decorated routes | **NOT run**; OpenAPI check runs inside Docker only |
| Docker build, compose, healthchecks, `depends_on` conditions, pinned versions exist | – | **NOT verified** — no Docker, no network |
| Real Mailpit API shapes (`/api/v1/messages`, `/message/{id}/raw`, `DELETE /api/v1/messages`) | fake written from knowledge of the API | **NOT verified** against a real Mailpit |
| Live RSS feeds, Groq, real SMTP provider | – | **NOT verified** (no network); use `check-feeds`, `check-smtp`, `send-test` |

**Web interface :**

| Claim | How verified | Status |
|---|---|---|
| Every UI page renders against the **real FastAPI app** (SQLite, real worker-free stack, SMTP stand-in) with no exception | Streamlit 1.62 headless `AppTest` driving all 7 pages | **Verified** |
| Forms and buttons change real state: add subscriber (incl. validation + duplicate error), create schedule, Run now, approve / reject (reason required), fetch articles | `AppTest` clicks, then the API state was read back | **Verified** |
| 16 UI tests (stub API: empty data, API down, stale worker, real-mail tick-box, helpers, full navigation shell) | `pytest` in a clean venv built from `ui/requirements.txt` | **Verified** (`./run.sh test-ui` runs them in Docker) |
| UI only calls API routes that exist, never imports a DB driver, is published on 127.0.0.1 only and gets no `.env` | `tests/test_ui_contract.py`; 4 deliberate breakages were each caught | **Verified** |
| A real Streamlit server starts and answers `/_stcore/health` | `streamlit run app.py` headless | **Verified** |
| Clicking between pages in a browser, visual layout of charts | – | **NOT verified** — no browser available. `AppTest.switch_page` does not support `st.navigation`, so page switching was only checked by rendering each page on its own and the full shell once. |
| Docker build of the `ui` image, its healthcheck, `depends_on: api` | – | **NOT verified** — no Docker |
| UI against the PostgreSQL-backed stack | – | **NOT verified** — the API layer is unchanged, but only SQLite was behind it |

If `./run.sh test` or `./run.sh smoke` fails on your machine, paste the output: the most likely culprits are the unverified rows above.

## Layout

```
run.sh  docker-compose.yml  Dockerfile  requirements.txt  .env.example  sources.yaml  alembic.ini
app/        config, db, clock, topics, feeds, ingest, llm (mock), pipeline, service, scheduling, worker, mailer, email_build, api, cli, smoke
migrations/ env.py, versions/0001_initial.py, sql/0001_initial.sql
tests/      unittest suites + fake_smtp.py (real SMTP server) + fake_mailpit.py + test_ui_contract.py (UI only calls real API routes)
ui/         Streamlit web interface: app.py (navigation), page_*.py, common.py (API client), Dockerfile, requirements.txt, tests/
```
