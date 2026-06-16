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
        if job.result.raw_path is not None:
            result["raw_path"] = str(job.result.raw_path)
        if include_markdown:
            result["markdown"] = job.result.markdown
            result["raw"] = job.result.raw
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
