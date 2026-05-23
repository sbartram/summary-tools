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
