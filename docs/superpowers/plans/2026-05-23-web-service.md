# Local Web Service (REST Job API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a localhost-only FastAPI web service that runs the existing summarize pipelines as background jobs (submit → poll), supporting parallel requests, driven by a CLI/Chrome-extension client.

**Architecture:** A new `server.py` imports the pipeline functions from `summarize.py` and runs them on a `ThreadPoolExecutor` (work is I/O-bound, so threads give real parallelism). Jobs live in an in-memory store; `POST /jobs/{youtube,article}` enqueue and return a `job_id`; `GET /jobs/{id}` returns status, a growing progress log, and the result. `summarize.py` gets two minimal, behavior-preserving changes: an injectable `log` callback (default = current stderr printing) and a structured `SummaryResult` return type.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, Pydantic, `concurrent.futures.ThreadPoolExecutor`, existing `anthropic`/`yt-dlp`/`trafilatura` pipeline.

**Testing note:** This repo has no test framework — manual smoke verification is the bar (per CLAUDE.md). Each task's verification uses import/inspect checks, real CLI runs, or curl against the running server. Commit style: lowercase imperative, no `feat:` prefix; append the `Co-Authored-By` trailer.

**Reference spec:** `docs/superpowers/specs/2026-05-23-web-service-design.md`

---

## File Structure

- **Modify `summarize.py`** — add `_default_log`, `SummaryResult`; thread a `log` callback through `summarize`, `summarize_article`, `process_url`, `process_article`, `process_transcript_file`; change the three `process_*` return types to `SummaryResult | None`.
- **Create `server.py`** — config, `Job`/`JobStore`, FastAPI app with lifespan (key check + client), worker function, routes, `main()`.
- **Modify `pyproject.toml`** — `server` optional-dependency group, `summarize-server` script, wheel include.
- **Modify `requirements.txt`** — add `fastapi`, `uvicorn` for the dev-venv flow.
- **Modify `README.md` / `CLAUDE.md`** — document the server.

---

## Task 1: Add `_default_log` and `SummaryResult` to `summarize.py`

**Files:**
- Modify: `summarize.py` (imports near line 33; new defs after the constants block ~line 52)

- [ ] **Step 1: Add the `dataclass` import**

In the stdlib import block (currently lines 29-34), add `dataclasses`. After:

```python
import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
```

- [ ] **Step 2: Add `_default_log` and `SummaryResult` after the constants block**

Insert immediately after the `API_MAX_RETRIES = 5 ...` line (line 51) and before the `# ---------- URL & metadata ----------` comment:

```python


def _default_log(msg: str) -> None:
    """Default progress sink: print to stderr exactly as the CLI always has."""
    print(msg, file=sys.stderr)


@dataclass
class SummaryResult:
    """Structured result of a process_* pipeline run."""
    path: Path
    markdown: str
    meta: dict
```

- [ ] **Step 3: Verify the module still imports and the new names exist**

Run: `.venv/bin/python -c "from summarize import _default_log, SummaryResult; print('ok')"`
Expected: `ok` (no ImportError / SyntaxError)

- [ ] **Step 4: Commit**

```bash
git add summarize.py
git commit -m "add log callback and SummaryResult to summarize

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Thread `log` through `summarize` and `summarize_article`

**Files:**
- Modify: `summarize.py:591-630` (`summarize`), `summarize.py:633-665` (`summarize_article`)

- [ ] **Step 1: Update `summarize` signature and its stderr prints**

Change the signature (line 591) to add a keyword-only `log` param, and replace the two `print(..., file=sys.stderr)` calls (lines 606-610 and 620) with `log(...)`. Final function:

```python
def summarize(
    chunks: list[dict], meta: dict, client: Anthropic, model: str,
    *, log=_default_log,
) -> str:
    if len(chunks) == 1:
        prompt = SINGLE_PASS_PROMPT.format(
            title=meta["title"],
            channel=meta["channel"],
            duration=fmt_ts(meta["duration"]),
            published=meta.get("published") or "—",
            summarized=datetime.now().strftime("%Y-%m-%d"),
            url=meta["url"],
            text=chunks[0]["text"],
        )
        return call_claude(client, model, prompt, max_tokens=4000)

    section_summaries = []
    for i, ch in enumerate(chunks, 1):
        log(
            f"  · section {i}/{len(chunks)} "
            f"({fmt_ts(ch['start'])}–{fmt_ts(ch['end'])})"
        )
        prompt = CHUNK_PROMPT.format(
            title=meta["title"],
            channel=meta["channel"],
            start=fmt_ts(ch["start"]),
            end=fmt_ts(ch["end"]),
            text=ch["text"],
        )
        section_summaries.append(call_claude(client, model, prompt, max_tokens=1500))

    log("  · synthesizing")
    prompt = SYNTHESIS_PROMPT.format(
        title=meta["title"],
        channel=meta["channel"],
        duration=fmt_ts(meta["duration"]),
        published=meta.get("published") or "—",
        summarized=datetime.now().strftime("%Y-%m-%d"),
        url=meta["url"],
        sections="\n\n---\n\n".join(section_summaries),
    )
    return call_claude(client, model, prompt, max_tokens=4000)
```

- [ ] **Step 2: Update `summarize_article` signature and its stderr prints**

Change the signature (line 633) to add `*, log=_default_log` and replace the two `print(..., file=sys.stderr)` calls (lines 651 and 660) with `log(...)`. Final function:

```python
def summarize_article(
    chunks: list[dict], meta: dict, client: Anthropic, model: str,
    *, log=_default_log,
) -> str:
    fields = {
        "title": meta["title"],
        "site": meta.get("site") or "—",
        "author": meta.get("author") or "—",
        "published": meta.get("published") or "—",
        "summarized": datetime.now().strftime("%Y-%m-%d"),
        "url": meta["url"],
    }
    if len(chunks) == 1:
        prompt = ARTICLE_SINGLE_PASS_PROMPT.format(text=chunks[0]["text"], **fields)
        return call_claude(client, model, prompt, max_tokens=4000)

    section_summaries = []
    for i, ch in enumerate(chunks, 1):
        label = ch["title"] or "(intro)"
        log(f"  · section {i}/{len(chunks)}: {label}")
        prompt = ARTICLE_CHUNK_PROMPT.format(
            title=fields["title"],
            site=fields["site"],
            section_title=ch["title"] or "(introduction)",
            text=ch["text"],
        )
        section_summaries.append(call_claude(client, model, prompt, max_tokens=1500))

    log("  · synthesizing")
    prompt = ARTICLE_SYNTHESIS_PROMPT.format(
        sections="\n\n---\n\n".join(section_summaries),
        **fields,
    )
    return call_claude(client, model, prompt, max_tokens=4000)
```

- [ ] **Step 3: Verify import + signatures**

Run:
```bash
.venv/bin/python -c "import inspect, summarize; print('log' in inspect.signature(summarize.summarize).parameters); print('log' in inspect.signature(summarize.summarize_article).parameters)"
```
Expected: two lines, both `True`.

- [ ] **Step 4: Commit**

```bash
git add summarize.py
git commit -m "route summarize progress through injectable log callback

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Thread `log` + `SummaryResult` through the `process_*` functions

**Files:**
- Modify: `summarize.py:737-764` (`process_url`), `:767-797` (`process_transcript_file`), `:800-821` (`process_article`)

- [ ] **Step 1: Rewrite `process_url`**

Replace lines 737-764 with (adds `*, log=_default_log`, replaces every `print(..., file=sys.stderr)` with `log(...)`, passes `log=log` into `summarize`, returns `SummaryResult`):

```python
def process_url(
    url: str, client: Anthropic, model: str, out_dir: Path,
    *, log=_default_log,
) -> SummaryResult | None:
    log(f"\n→ {url}")
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        log(f"  ✗ {e}")
        return None

    log("  · fetching metadata")
    meta = fetch_metadata(url)
    log(f"  · title: {meta['title']}")

    log("  · fetching transcript")
    segments = fetch_transcript(video_id)
    if not segments:
        log("  ✗ no transcript available (captions disabled or absent)")
        return None

    chunks = chunk_transcript(segments)
    log(f"  · {len(segments)} segments → {len(chunks)} chunk(s)")

    summary = summarize(chunks, meta, client, model, log=log)
    path = write_summary(summary, meta, out_dir)
    log(f"  ✓ wrote {path}")
    return SummaryResult(path=path, markdown=summary, meta=meta)
```

- [ ] **Step 2: Rewrite `process_transcript_file`**

Replace lines 767-797 with:

```python
def process_transcript_file(
    path: Path,
    client: Anthropic,
    model: str,
    out_dir: Path,
    title: str | None,
    source: str | None,
    *, log=_default_log,
) -> SummaryResult | None:
    log(f"\n→ {path}")
    segments = parse_transcript_file(path)
    if not segments:
        log("  ✗ no segments parsed (expected '[MM:SS --> MM:SS] text' lines)")
        return None

    duration = int(segments[-1]["start"] + segments[-1]["duration"])
    meta = {
        "title": title or title_from_filename(path),
        "channel": "Local transcript",
        "duration": duration,
        "published": None,
        "url": source or "",
    }
    log(f"  · title: {meta['title']}")

    chunks = chunk_transcript(segments)
    log(f"  · {len(segments)} segments → {len(chunks)} chunk(s)")

    summary = summarize(chunks, meta, client, model, log=log)
    path_out = write_summary(summary, meta, out_dir)
    log(f"  ✓ wrote {path_out}")
    return SummaryResult(path=path_out, markdown=summary, meta=meta)
```

- [ ] **Step 3: Rewrite `process_article`**

Replace lines 800-821 with (note `log` comes after the existing `force_playwright` keyword-only arg):

```python
def process_article(
    url: str, client: Anthropic, model: str, out_dir: Path,
    *, force_playwright: bool = False, log=_default_log,
) -> SummaryResult | None:
    log(f"\n→ {url}")
    log("  · fetching article")
    try:
        meta, text = fetch_article(url, force_playwright=force_playwright)
    except RuntimeError as e:
        log(f"  ✗ {e}")
        return None

    log(f"  · title: {meta['title']}")
    log(f"  · {meta['word_count']} words")

    chunks = chunk_article(text)
    log(f"  · {len(chunks)} chunk(s)")

    summary = summarize_article(chunks, meta, client, model, log=log)
    path = write_summary(summary, meta, out_dir)
    log(f"  ✓ wrote {path}")
    return SummaryResult(path=path, markdown=summary, meta=meta)
```

- [ ] **Step 4: Verify import + return annotations + CLI wiring untouched**

Run:
```bash
.venv/bin/python -c "import inspect, summarize; print(inspect.signature(summarize.process_url).return_annotation); print(inspect.signature(summarize.process_article).return_annotation)"
```
Expected: both print `SummaryResult | None`.

- [ ] **Step 5: Verify CLI behavior is byte-for-byte unchanged (real run)**

This is the guard for the "CLI unchanged" requirement. Run a short article end-to-end (small cost, requires `ANTHROPIC_API_KEY`):

Run: `python summarize.py --article https://blog.samaltman.com/what-i-wish-someone-had-told-me`
Expected: same stderr progress lines as before (`→ …`, `  · fetching article`, `  · title: …`, `  · N chunk(s)`, possibly `  · section i/N: …`, `  · synthesizing`, `  ✓ wrote summaries/…md`) and a new `.md` file in `./summaries/`. Confirm the run exits 0 and the file looks correct.

- [ ] **Step 6: Commit**

```bash
git add summarize.py
git commit -m "return SummaryResult and accept log callback in process_* functions

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Packaging — `pyproject.toml` and `requirements.txt`

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`

- [ ] **Step 1: Add the `server` optional-dependency group**

In `pyproject.toml`, under `[project.optional-dependencies]`, add the `server` line:

```toml
[project.optional-dependencies]
transcribe = ["pywhispercpp"]
playwright = ["playwright"]
server = ["fastapi", "uvicorn[standard]"]
```

- [ ] **Step 2: Add the `summarize-server` script entry**

Under `[project.scripts]`:

```toml
[project.scripts]
summarize = "summarize:main"
summarize-server = "server:main"
```

- [ ] **Step 3: Include `server.py` in the wheel**

Change the wheel include line:

```toml
[tool.hatch.build.targets.wheel]
only-include = ["summarize.py", "server.py"]
```

- [ ] **Step 4: Add the dev-venv deps to `requirements.txt`**

Append `fastapi` and `uvicorn` (matching how `pywhispercpp`/`playwright` are listed for the venv flow):

```
fastapi
uvicorn[standard]
```

- [ ] **Step 5: Verify `pyproject.toml` parses**

Run: `.venv/bin/python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['optional-dependencies']['server']); print(d['project']['scripts']['summarize-server'])"`
Expected: `['fastapi', 'uvicorn[standard]']` then `server:main`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt
git commit -m "add server optional-dependency group and summarize-server entry point

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `server.py` — config, `Job`, `JobStore`, helpers

**Files:**
- Create: `server.py`

- [ ] **Step 0: Install FastAPI + Uvicorn into the dev venv (needed from here on)**

Run: `VIRTUAL_ENV=.venv uv pip install fastapi 'uvicorn[standard]'`
Expected: installs succeed (`.venv` has no `pip`; this is the documented way to add packages — see CLAUDE.md). Idempotent if already installed.

- [ ] **Step 1: Write the top of `server.py` (imports, config, helpers, store)**

Create `server.py` with exactly this content (the FastAPI app + routes are appended in Task 6):

```python
#!/usr/bin/env python3
"""
server.py — Local HTTP job API for the summarize pipelines.

Single-user, localhost-only. Submit a URL, get a job_id, poll for status +
progress log + result markdown. Imports the pipeline functions from
summarize.py and runs them on a thread pool (the work is I/O-bound).

Run (installed):   summarize-server
Run (dev venv):    .venv/bin/python server.py     # or: uvicorn server:app
Docs:              http://127.0.0.1:8723/docs
Requires:          ANTHROPIC_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from summarize import (
    API_MAX_RETRIES,
    DEFAULT_MODEL,
    SummaryResult,
    process_article,
    process_url,
)

# ---------- Config (env, overridable by CLI flags in main) ----------

HOST = os.environ.get("SUMMARIZE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SUMMARIZE_PORT", "8723"))
WORKERS = int(os.environ.get("SUMMARIZE_WORKERS", "2"))
OUT_DIR = Path(os.environ.get("SUMMARIZE_OUT_DIR", "./summaries"))
SERVER_MODEL = os.environ.get("SUMMARIZE_MODEL", DEFAULT_MODEL)

# CORS: a Chrome extension's origin is chrome-extension://<id>; localhost for dev.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "SUMMARIZE_CORS_ORIGINS", "http://localhost,http://127.0.0.1"
    ).split(",")
    if o.strip()
]

# ---------- Job model + in-memory store ----------


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    id: str
    kind: str            # "youtube" | "article"
    source: str
    model: str
    status: str = "queued"   # queued -> running -> succeeded | failed
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log: list[str] = field(default_factory=list)
    result: SummaryResult | None = None
    error: str | None = None


class JobStore:
    """Thread-safe in-memory job store. Lost on restart; .md files persist."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str, source: str, model: str) -> Job:
        with self._lock:
            job = Job(id="j_" + secrets.token_hex(4), kind=kind, source=source, model=model)
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.log.append(line)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                for key, value in fields.items():
                    setattr(job, key, value)


_LOG_PREFIXES = ("· ", "✓ ", "✗ ", "→ ")


def _clean_log_line(msg: str) -> str:
    """Strip the CLI's decorative prefixes so job logs read as plain lines."""
    s = msg.strip()
    for prefix in _LOG_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s


def _job_to_dict(job: Job, *, include_markdown: bool = True) -> dict:
    result = None
    if job.result is not None:
        result = {"path": str(job.result.path), "title": job.result.meta.get("title")}
        if include_markdown:
            result["markdown"] = job.result.markdown
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "source": job.source,
        "model": job.model,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "log": list(job.log),
        "result": result,
        "error": job.error,
    }
```

- [ ] **Step 2: Verify the partial module imports**

Run: `.venv/bin/python -c "import server; s=server.JobStore(); j=s.create('article','http://x','m'); print(j.id.startswith('j_')); print(server._clean_log_line('  · fetching article'))"`
Expected: `True` then `fetching article`.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "add server config, Job model, and in-memory JobStore

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `server.py` — FastAPI app, worker, routes, `main`

**Files:**
- Modify: `server.py` (append to the file from Task 5)

- [ ] **Step 1: Append the request models, app, worker, routes, and `main`**

Append to the end of `server.py`:

```python

# ---------- App, worker, routes ----------

client: Anthropic | None = None
store = JobStore()
executor = ThreadPoolExecutor(max_workers=WORKERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = Anthropic(max_retries=API_MAX_RETRIES)
    yield


app = FastAPI(title="summary-tools server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)


class YoutubeJobRequest(BaseModel):
    url: str
    model: str | None = None


class ArticleJobRequest(BaseModel):
    url: str
    model: str | None = None
    playwright: bool = False


def _run_job(job_id: str, runner) -> None:
    """Execute one job on a worker thread. Never lets an exception escape."""
    store.update(job_id, status="running", started_at=_now())

    def log(msg: str) -> None:
        store.append_log(job_id, _clean_log_line(msg))

    try:
        result = runner(log)
        if result is None:
            job = store.get(job_id)
            reason = job.log[-1] if job and job.log else "summarization failed"
            store.update(job_id, status="failed", error=reason, finished_at=_now())
        else:
            store.update(job_id, status="succeeded", result=result, finished_at=_now())
    except Exception as e:  # noqa: BLE001 - worker must survive any pipeline error
        store.append_log(job_id, traceback.format_exc())
        store.update(job_id, status="failed", error=str(e), finished_at=_now())


@app.post("/jobs/youtube", status_code=202)
def submit_youtube(req: YoutubeJobRequest):
    model = req.model or SERVER_MODEL
    job = store.create("youtube", req.url, model)
    executor.submit(
        _run_job, job.id,
        lambda log: process_url(req.url, client, model, OUT_DIR, log=log),
    )
    return {"id": job.id, "kind": job.kind, "status": job.status, "source": job.source}


@app.post("/jobs/article", status_code=202)
def submit_article(req: ArticleJobRequest):
    model = req.model or SERVER_MODEL
    job = store.create("article", req.url, model)
    executor.submit(
        _run_job, job.id,
        lambda log: process_article(
            req.url, client, model, OUT_DIR, force_playwright=req.playwright, log=log
        ),
    )
    return {"id": job.id, "kind": job.kind, "status": job.status, "source": job.source}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_dict(job)


@app.get("/jobs")
def list_jobs():
    return [_job_to_dict(j, include_markdown=False) for j in store.list()]


@app.get("/healthz")
def healthz():
    return {"status": "ok", "workers": WORKERS, "jobs": len(store.list())}


def main() -> None:
    global OUT_DIR, SERVER_MODEL, executor
    parser = argparse.ArgumentParser(description="Run the summary-tools web service.")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--model", default=SERVER_MODEL)
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    OUT_DIR = Path(args.out_dir)
    SERVER_MODEL = args.model
    if args.workers != WORKERS:
        executor = ThreadPoolExecutor(max_workers=args.workers)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the full module imports and routes are registered**

Run:
```bash
.venv/bin/python -c "import server; print(sorted({r.path for r in server.app.routes if r.path.startswith('/')}))"
```
Expected includes: `/healthz`, `/jobs`, `/jobs/article`, `/jobs/youtube`, `/jobs/{job_id}`.

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "add FastAPI app, job worker, and routes to server

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Install dev deps and run end-to-end smoke verification

**Files:** none (verification only)

- [ ] **Step 1: Start the server in the background**

(FastAPI/Uvicorn were installed in Task 5 Step 0.)

Run: `ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" .venv/bin/python server.py --port 8723 &`
Then: `sleep 2 && curl -s http://127.0.0.1:8723/healthz`
Expected: `{"status":"ok","workers":2,"jobs":0}`

- [ ] **Step 2: Submit an article job and poll to completion**

```bash
JID=$(curl -s -X POST http://127.0.0.1:8723/jobs/article \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://blog.samaltman.com/what-i-wish-someone-had-told-me"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "job: $JID"
for i in $(seq 1 60); do
  STATUS=$(curl -s http://127.0.0.1:8723/jobs/$JID | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "  $STATUS"; [ "$STATUS" = "succeeded" -o "$STATUS" = "failed" ] && break; sleep 3
done
curl -s http://127.0.0.1:8723/jobs/$JID | python3 -m json.tool | head -40
```
Expected: status transitions `queued`/`running` → `succeeded`; final JSON has a non-empty `result.markdown`, a `result.path` under `summaries/`, and a populated `log` array. Confirm the `.md` file exists on disk.

- [ ] **Step 3: Submit a YouTube job and confirm progress log accrues**

```bash
JID=$(curl -s -X POST http://127.0.0.1:8723/jobs/youtube \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=<SHORT_CAPTIONED_VIDEO>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
sleep 4; curl -s http://127.0.0.1:8723/jobs/$JID | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status']); print(d['log'])"
```
Replace `<SHORT_CAPTIONED_VIDEO>` with a real short captioned video id. Expected: `log` contains lines like `fetching metadata`, `fetching transcript`, eventually `synthesizing`/`wrote …`; job reaches `succeeded`.

- [ ] **Step 4: Verify parallelism + validation + 404**

```bash
# fire two at once, confirm both run concurrently (overlapping timestamps)
curl -s -X POST http://127.0.0.1:8723/jobs/article -H 'Content-Type: application/json' -d '{"url":"https://blog.samaltman.com/productivity"}' >/dev/null &
curl -s -X POST http://127.0.0.1:8723/jobs/article -H 'Content-Type: application/json' -d '{"url":"https://blog.samaltman.com/how-to-be-successful"}' >/dev/null &
wait
curl -s http://127.0.0.1:8723/jobs | python3 -c "import sys,json; print([(j['status']) for j in json.load(sys.stdin)])"
# validation error (missing url) -> 422
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8723/jobs/article -H 'Content-Type: application/json' -d '{}'
# unknown job -> 404
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8723/jobs/nope
```
Expected: two jobs listed and both progress; `422` for the empty body; `404` for the unknown id.

- [ ] **Step 5: Confirm the CLI still works unchanged**

Run: `python summarize.py --completion >/dev/null && echo "cli import ok"`
Expected: `cli import ok` (no import/runtime error from the refactor; the real-run CLI guard was Task 3 Step 5).

- [ ] **Step 6: Stop the background server**

Run: `kill %1 2>/dev/null; pkill -f "server.py --port 8723" 2>/dev/null; echo stopped`
Expected: `stopped`.

---

## Task 8: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a "Web service" section to `README.md`**

Add after the install/usage section:

```markdown
## Web service (local REST job API)

Run the pipelines behind an HTTP job API (for a CLI client or browser extension):

    uv tool install '.[server]'      # or: VIRTUAL_ENV=.venv uv pip install fastapi 'uvicorn[standard]'
    summarize-server                 # binds 127.0.0.1:8723; needs ANTHROPIC_API_KEY

Submit a job, then poll for the result:

    curl -X POST localhost:8723/jobs/article -H 'Content-Type: application/json' -d '{"url":"https://example.com/post"}'
    # -> {"id":"j_ab12cd34","kind":"article","status":"queued",...}
    curl localhost:8723/jobs/j_ab12cd34        # status, progress log, and result.markdown

Endpoints: `POST /jobs/youtube`, `POST /jobs/article`, `GET /jobs/{id}`, `GET /jobs`, `GET /healthz`. Interactive docs at `/docs`.
Config via env: `SUMMARIZE_HOST`, `SUMMARIZE_PORT`, `SUMMARIZE_WORKERS`, `SUMMARIZE_OUT_DIR`, `SUMMARIZE_MODEL` (or the matching `summarize-server` flags).
```

- [ ] **Step 2: Add a server note to `CLAUDE.md` Architecture section**

Add this paragraph at the end of the "## Architecture" section of `CLAUDE.md`:

```markdown
**Web service** (`server.py`, `--server` extra). A FastAPI app that imports the pipeline functions and runs them as background jobs on a `ThreadPoolExecutor` (work is I/O-bound). `POST /jobs/{youtube,article}` enqueue and return a `job_id`; `GET /jobs/{id}` returns status + a progress log + `result.markdown`. Localhost-only, single-user, no auth. The pipeline's `process_*`/`summarize*` functions take an injectable `log` callback (default = stderr, preserving CLI output) and `process_*` return a `SummaryResult(path, markdown, meta)`. Transcript-file mode stays CLI-only. See `docs/superpowers/specs/2026-05-23-web-service-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "document the local web service

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Done

All endpoints implemented, parallel jobs verified, CLI behavior preserved, docs updated. The service is reachable only on `127.0.0.1:8723` and requires `ANTHROPIC_API_KEY`.
