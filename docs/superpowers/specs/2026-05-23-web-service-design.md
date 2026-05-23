# Local Web Service (REST Job API)

**Date:** 2026-05-23
**Status:** Approved for planning

## Problem

`summarize.py` is CLI-only. To drive it from a Chrome browser extension (and a local CLI client), it needs an HTTP interface. Summarizing is long-running — a short article takes seconds, but a 1-hour video runs ~5 sequential Claude calls and takes minutes — so a plain blocking request risks browser/proxy timeouts and gives no progress feedback. The service must also handle several requests in parallel.

## Goals

1. Expose the existing summarize pipelines over a local HTTP REST API.
2. Use a **job model**: submit returns a `job_id` immediately; the client polls for status, progress, and result. Fits the extension's UX (progress UI, no long-held connections).
3. Run **multiple jobs in parallel**.
4. Reuse the existing pipeline functions in-process — no duplication, no shelling out.
5. Keep the CLI's behavior byte-for-byte unchanged.
6. Keep the web stack optional — base install must not require it.

## Non-Goals

- Authentication / multi-user support. Single user, localhost-bound.
- Transcript-file mode over HTTP (`--transcript-file` stays CLI-only — see Scope).
- Persistent job storage / a database. In-memory store; `.md` files remain the durable artifact.
- Streaming (SSE) responses. The job model + polled log is the progress mechanism.
- Exposing the service beyond `127.0.0.1`. If that ever happens, auth + rate limiting is a separate hardening pass.
- An SSRF filter on article fetches (see Security — proportionality rationale).

## Design

### Architecture

A FastAPI app (`server.py`) imports the pipeline functions from `summarize.py` and runs them on a pool of worker threads. The CLI's filesystem/stderr boundary is replaced, for the service, by a Python function-return boundary that already carries the structured data the API needs (summary markdown + output path + meta).

```
POST /jobs/youtube ─► enqueue Job(queued) ─► ThreadPoolExecutor ─► process_url(..., log=cb)
                          │                                              │
                          └─► return 202 {id}                            ├─► summarize() → markdown
                                                                         └─► write_summary() → path
GET /jobs/{id} ─► read Job from in-memory store ─► {status, log[], result}
```

Threads (not async, not multiprocessing) because every stage is **I/O-bound** — network to YouTube/article hosts and the Claude API — so threads give real parallelism despite the GIL, while letting us call the existing synchronous pipeline directly. One shared `Anthropic` client is used across all workers.

### Changes to `summarize.py` (minimal seam)

Two behavior-preserving changes; the CLI path is unaffected.

**1. Injectable `log` callback.** The pipeline's progress/error `print(..., file=sys.stderr)` calls in `process_url`, `process_article`, `process_transcript_file`, `summarize`, and `summarize_article` route through a `log` callable threaded into those functions, **defaulting to the current stderr-printing behavior**. The server passes a callback that appends to the job's `log` list.

- Signature additions, e.g. `def process_url(url, client, model, out_dir, *, log=_default_log) -> SummaryResult | None`, and `summarize(chunks, meta, client, model, *, log=_default_log)`.
- `_default_log(msg)` prints to stderr exactly as today (preserving the `  · `/`  ✓ `/`  ✗ ` prefixes the CLI emits).
- This avoids the thread-unsafe alternative of redirecting global `sys.stderr`, which would scramble logs across concurrent jobs.

**2. Structured return type.** `process_url` / `process_article` / `process_transcript_file` change return type from `Path | None` to `SummaryResult | None`, where:

```python
@dataclass
class SummaryResult:
    path: Path
    markdown: str
    meta: dict
```

`summarize()`/`summarize_article()` already produce the markdown string before `write_summary()` writes it, so the `process_*` functions return all three without re-reading disk. `main()` only checks truthiness of the return value, so the CLI is unaffected.

### Components (`server.py`)

**`Job`** — dataclass / Pydantic model in the in-memory store:
`id`, `kind` (`"youtube"`/`"article"`), `source`, `model`, `status` (`queued`→`running`→`succeeded`/`failed`), `created_at`, `started_at`, `finished_at`, `log: list[str]`, `result: SummaryResult | None`, `error: str | None`.

**`JobStore`** — `dict[str, Job]` guarded by a `threading.Lock`. Methods: `create()`, `get()`, `list()`, and an `update()` context that workers use to mutate a job under the lock. Job ids are short random tokens (e.g. `j_` + `secrets.token_hex(4)`).

**Worker function** — submitted to the `ThreadPoolExecutor`:
1. Set `status=running`, `started_at`.
2. Call the matching `process_*` with `log=` a callback that appends to `job.log` (under the lock).
3. On a returned `SummaryResult` → `status=succeeded`, store `result`.
4. On returned `None` → `status=failed`, `error` = last meaningful log line (the reason the pipeline printed before bailing).
5. On any exception → `status=failed`, `error=str(e)`, append the traceback to `log`. **Never lets the exception escape the worker** so the pool thread survives.

**FastAPI app** — routes below, CORS middleware, startup check for `ANTHROPIC_API_KEY`, one module-level `Anthropic(max_retries=API_MAX_RETRIES)` client.

### API

Request bodies validated by Pydantic (missing/invalid `url` → automatic `422` before any job is created).

| Method | Path | Body | Purpose |
|---|---|---|---|
| `POST` | `/jobs/youtube` | `{url: str, model?: str}` | Enqueue a YouTube summary |
| `POST` | `/jobs/article` | `{url: str, model?: str, playwright?: bool}` | Enqueue an article summary |
| `GET`  | `/jobs/{id}` | — | Poll one job |
| `GET`  | `/jobs` | — | List jobs (extension history/debug) |
| `GET`  | `/healthz` | — | Liveness |

**Submit response** — `202 Accepted`:
```json
{ "id": "j_a1b2c3d4", "kind": "youtube", "status": "queued", "source": "https://..." }
```

**Poll response** — `GET /jobs/{id}` → `200` if the job exists (job *failure* is reported in the body, not via HTTP status); `404` for an unknown id:
```json
{
  "id": "j_a1b2c3d4", "kind": "youtube", "status": "succeeded",
  "source": "https://...", "model": "claude-sonnet-4-6",
  "created_at": "2026-05-23T18:00:00Z", "started_at": "...", "finished_at": "...",
  "log": ["fetching metadata", "title: ...", "section 2/4", "synthesizing"],
  "result": { "markdown": "# ...", "path": "summaries/2026-05-23_....md", "title": "..." },
  "error": null
}
```
While `running`: `result` is `null`, `log` grows as the client polls (the progress feed). On `failed`: `result` is `null`, `error` carries the reason.

The JSON `result` object is `{markdown, path, title}`, mapped from the worker's `SummaryResult`: `markdown` and `path` directly, `title` pulled from `SummaryResult.meta["title"]`. (`path` is serialized as a string.)

Default model when the request omits `model` is `DEFAULT_MODEL` (`claude-sonnet-4-6`), overridable per-request and via `SUMMARIZE_MODEL`.

### Scope: transcript-file mode is CLI-only

No `/jobs/transcript` endpoint. The CLI already handles local transcript files directly, and the browser extension has no local file path to send. Supporting it over HTTP would mean either uploading transcript text or trusting a server-side path (traversal risk) — neither is justified now. Easy to add later (`POST /jobs/transcript {text, title?, source?, model?}`) if a need appears.

### Configuration

Env vars, each with a matching `summarize-server` CLI flag:

| Setting | Env | Default |
|---|---|---|
| Bind host | `SUMMARIZE_HOST` | `127.0.0.1` |
| Port | `SUMMARIZE_PORT` | `8723` |
| Worker threads | `SUMMARIZE_WORKERS` | `2` |
| Output dir | `SUMMARIZE_OUT_DIR` | `./summaries` |
| Default model | `SUMMARIZE_MODEL` | `claude-sonnet-4-6` |
| API key | `ANTHROPIC_API_KEY` | *(required, existing)* |

**Running:** `summarize-server` (entry point → `uvicorn.run(app, host, port)`), or `uvicorn server:app --reload` in dev. Auto-docs at `/docs`.

### Packaging

`pyproject.toml`:
```toml
[project.optional-dependencies]
transcribe = ["pywhispercpp"]
playwright = ["playwright"]
server = ["fastapi", "uvicorn[standard]"]

[project.scripts]
summarize = "summarize:main"
summarize-server = "server:main"

[tool.hatch.build.targets.wheel]
only-include = ["summarize.py", "server.py"]
```

`requirements.txt`: append `fastapi` and `uvicorn` for the dev-venv flow (matching the `pywhispercpp`/`playwright` precedent).

Install: `uv tool install '.[server]'`.

### Networking & Security

Single-user localhost tool; controls are proportionate.

- **Bind `127.0.0.1` only** (never `0.0.0.0`). This — not CORS — is the control that makes the service unreachable from other machines.
- **CORS:** a Chrome extension's fetch origin is `chrome-extension://<extension-id>`. Allowlist is configurable; default allows the `chrome-extension://` scheme plus `http://localhost` / `http://127.0.0.1`. No cookies/credentials are used, so no credentialed-CORS constraints. Blanket `*` is avoided but is a one-line override for dev.
- **No auth** — consistent with localhost-bound single-user scope.
- **SSRF:** `/jobs/article` fetches arbitrary user-supplied URLs. This is inherent to the tool and identical to what the CLI already does; on a localhost-only, single-user service it is not a new exposure. An SSRF filter (blocking private IPs) would add false-positive friction without closing a real gap here, so it is intentionally **not** added. Documented as an explicit decision; revisit if the service is ever exposed beyond localhost.
- Job `log`/`error` text is returned as JSON data, not rendered as HTML by the API — no injection surface in the service itself.

### Error Handling Matrix

| Condition | Behavior |
|---|---|
| Missing/invalid request body (no `url`, wrong type) | FastAPI/Pydantic `422` at submit, before a job exists |
| Unknown job id on `GET /jobs/{id}` | `404` |
| Expected pipeline failure (no transcript, fetch blocked, bad video id) | Job `failed`; `error` = reason captured via the `log` callback |
| Unexpected exception in a worker | Caught; job `failed`; `error=str(e)`; traceback appended to `log`; pool thread survives |
| `ANTHROPIC_API_KEY` not set | Server refuses to start (fail fast, mirroring `main()`) — does not start then fail every job |

## Test Plan

Manual smoke verification (no test framework in this repo — matches CLAUDE.md):

1. **Liveness** — start `summarize-server`; `GET /healthz` returns OK.
2. **Article happy path** — `POST /jobs/article` with a known-good blog URL; poll until `succeeded`; confirm `result.markdown` is present and a file landed in `./summaries/`.
3. **YouTube + progress** — `POST /jobs/youtube` with a short captioned video; confirm progress lines (`fetching transcript`, `section N/M`, `synthesizing`) accumulate in `log` while `running`.
4. **Parallelism** — fire two jobs simultaneously; confirm both run concurrently (overlapping `log` timestamps), and a separate `python summarize.py <url>` CLI run still works unchanged.
5. **Validation error** — `POST /jobs/article` with no `url`; confirm `422`, no job created.
6. **Pipeline failure surfaces** — `POST /jobs/youtube` with a caption-less video; confirm job `failed` with a clear `error`.
7. **Bad job id** — `GET /jobs/nope`; confirm `404`.

## Risks

- **In-memory store lost on restart** — accepted; the `.md` artifacts persist on disk. SQLite is a future option if job history matters.
- **Worker pool saturation** — with `SUMMARIZE_WORKERS=2`, a third concurrent job queues behind the first two. Acceptable for single-user use; configurable. Claude rate limits also argue for a small pool.
- **stderr `log` refactor regressions** — the callback default must reproduce current CLI output exactly. Covered by test 4 (CLI unchanged).
- **CORS extension id** — the extension's id isn't known until it's loaded; the `chrome-extension://` scheme allowance (or an env override with the concrete id) covers this.

## Out of Scope (Possible Follow-ups)

- `POST /jobs/transcript` for transcript text over HTTP.
- SQLite-backed persistent job store + history endpoint.
- SSE / streaming progress.
- Auth + rate limiting (only if exposed beyond localhost).
- Auto-detect endpoint (`POST /jobs` with YouTube-vs-article sniffing).
- Job cancellation (`DELETE /jobs/{id}`).
