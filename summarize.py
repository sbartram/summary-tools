#!/usr/bin/env python3
"""
summarize.py — Summarize YouTube videos and web articles with Claude.

Captions-only for video (no Whisper fallback). Web articles via trafilatura.
Uses Claude Sonnet for chunk summaries and final synthesis. Writes Markdown
with TL;DR, key takeaways, walkthrough, and optional quotes.

Setup:
    pip install youtube-transcript-api yt-dlp anthropic trafilatura
    export ANTHROPIC_API_KEY=sk-ant-...

Usage:
    python summarize.py <url>
    python summarize.py <url> --out-dir ./notes
    python summarize.py --batch urls.txt
    python summarize.py <url> --model claude-haiku-4-5-20251001
    python summarize.py --transcript-file path/to/transcript.txt [--title "..."] [--source "..."]
    python summarize.py --article https://example.com/post

Bash completion (post-install):
    source <(summarize --completion)             # current shell
    summarize --completion > ~/.summarize-completion.bash && \
        echo 'source ~/.summarize-completion.bash' >> ~/.bashrc
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
)
from yt_dlp import YoutubeDL


DEFAULT_MODEL = "claude-sonnet-4-6"
WINDOW_MINUTES = 15            # chunk size for long videos
OVERLAP_MINUTES = 1            # overlap between chunks
TIMESTAMP_EVERY_S = 30         # how often to inject [timestamp] markers
ARTICLE_WORD_THRESHOLD = 6000  # below this, articles go single-pass
ARTICLE_MAX_CHUNK_WORDS = 2500 # if H2-split leaves a chunk bigger than this, fall back to H2+H3
API_MAX_RETRIES = 5            # SDK exponential backoff on 408/409/429/5xx/connection errors


# ---------- URL & metadata ----------

def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|/shorts/|/embed/|/live/)([0-9A-Za-z_-]{11})",
        r"youtu\.be/([0-9A-Za-z_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    raise ValueError(f"Could not extract video ID from: {url}")


def fetch_metadata(url: str) -> dict:
    opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title", "Untitled"),
        "channel": info.get("uploader", "Unknown"),
        "duration": int(info.get("duration") or 0),
        "published": _normalize_publish_date(info.get("upload_date")),
        "url": url,
    }


# ---------- Transcript ----------

def fetch_transcript(video_id: str) -> list[dict] | None:
    """Return [{text, start, duration}, ...] or None if unavailable.

    Handles both youtube-transcript-api v1.x (instance.fetch) and
    v0.x (classmethod get_transcript).
    """
    try:
        try:
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id)
            return [
                {"text": s.text, "start": s.start, "duration": s.duration}
                for s in fetched.snippets
            ]
        except AttributeError:
            return YouTubeTranscriptApi.get_transcript(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except Exception as e:
        print(f"  ! transcript error: {e}", file=sys.stderr)
        return None


# ---------- Local transcript files ----------

# Whisper-style line: "[MM:SS --> MM:SS]  text" or "[H:MM:SS --> H:MM:SS]  text"
_WHISPER_LINE = re.compile(
    r"^\[(\d{1,2}(?::\d{2}){1,2})\s*-->\s*(\d{1,2}(?::\d{2}){1,2})\]\s*(.*)$"
)


def _parse_ts(ts: str) -> float:
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    h, m, s = parts
    return h * 3600 + m * 60 + s


def parse_transcript_file(path: Path) -> list[dict]:
    """Parse a whisper-style transcript file into [{text, start, duration}, ...]."""
    segments = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _WHISPER_LINE.match(line.strip())
        if not m:
            continue
        start = _parse_ts(m.group(1))
        end = _parse_ts(m.group(2))
        text = m.group(3).strip()
        if not text:
            continue
        segments.append({"text": text, "start": start, "duration": max(end - start, 0)})
    return segments


def title_from_filename(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"_?transcript$", "", stem, flags=re.IGNORECASE)
    return re.sub(r"[_\-]+", " ", stem).strip() or "Transcript"


# ---------- Web articles ----------

# Match Markdown ATX headings of level 1-3 at line start.
_ARTICLE_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)

# Some sites (e.g. Morningstar) return HTTP 202 bot-challenge responses to
# trafilatura's default UA but serve normally to a browser-shaped client.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def _meta_from_trafilatura(md, url: str, text: str) -> dict:
    """Build the article meta dict from a trafilatura metadata object."""
    title = (getattr(md, "title", None) if md else None) or "Untitled"
    return {
        "title": title,
        "author": getattr(md, "author", None) if md else None,
        "site": getattr(md, "sitename", None) if md else None,
        "published": getattr(md, "date", None) if md else None,
        "url": url,
        "word_count": len(text.split()),
    }


def fetch_article(url: str, *, force_playwright: bool = False) -> tuple[dict, str]:
    """Fetch a web article, returning (meta, markdown_text).

    Tries trafilatura first (cheap HTTP). On any RuntimeError, auto-falls
    back to Playwright for JS-rendered sites. Pass force_playwright=True
    to skip the trafilatura attempt and go straight to Chromium.
    """
    if force_playwright:
        return fetch_article_playwright(url)

    try:
        return _fetch_article_trafilatura(url)
    except RuntimeError as trafi_err:
        try:
            return fetch_article_playwright(url)
        except RuntimeError as pw_err:
            if "playwright not installed" in str(pw_err):
                raise RuntimeError(
                    f"{trafi_err} (install '[playwright]' extra to enable "
                    f"fallback for JS-rendered sites)"
                ) from trafi_err
            raise


def _fetch_article_trafilatura(url: str) -> tuple[dict, str]:
    """Download a web article via trafilatura and return (meta, markdown_text).

    Uses trafilatura for boilerplate-free body extraction. Output is
    Markdown so chunk_article can split on '##'/'###' headings directly.
    """
    try:
        from trafilatura import fetch_url, extract, extract_metadata
        from trafilatura.downloads import fetch_response
        from trafilatura.settings import use_config
    except ImportError as e:
        raise RuntimeError("trafilatura not installed. Run: pip install trafilatura") from e

    cfg = use_config()
    cfg.set("DEFAULT", "USER_AGENTS", _BROWSER_UA)

    downloaded = fetch_url(url, config=cfg)
    if downloaded is None:
        # fetch_url discards the HTTP status; refetch once for a useful error.
        resp = fetch_response(url, config=cfg)
        if resp is None:
            raise RuntimeError(f"failed to fetch {url}: no response (network error or timeout)")
        hint = " (bot challenge — site rejected automated request)" if resp.status == 202 else ""
        raise RuntimeError(f"failed to fetch {url}: HTTP {resp.status}{hint}")

    text = extract(
        downloaded,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
    )
    if not text:
        raise RuntimeError(f"no article content extracted from {url}")

    return _meta_from_trafilatura(extract_metadata(downloaded), url, text), text


def fetch_article_playwright(url: str) -> tuple[dict, str]:
    """Render a JS-heavy article with headless Chromium and extract content.

    Hands the rendered HTML to trafilatura so the same boilerplate stripping,
    Markdown conversion, and metadata extraction used by fetch_article apply.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright not installed. "
            "Run: uv tool install '.[playwright]' && playwright install chromium"
        ) from e
    from trafilatura import extract, extract_metadata

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            raise RuntimeError(
                f"failed to launch Chromium: {e}. Run: playwright install chromium"
            ) from e
        try:
            page = browser.new_page(user_agent=_BROWSER_UA)
            # Sites with continuous long-polling (X.com, ad-heavy news) never
            # reach networkidle. DOMContentLoaded + a brief settle covers them
            # without losing much on slower-rendering sites.
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            html = page.content()
        finally:
            browser.close()

    text = extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
    )
    if not text:
        raise RuntimeError(f"no article content extracted from {url} (after Playwright)")

    return _meta_from_trafilatura(extract_metadata(html), url, text), text


def _build_chunks(text: str, splits: list[tuple]) -> list[dict]:
    chunks: list[dict] = []
    if splits[0][0] > 0:
        preamble = text[: splits[0][0]].strip()
        if preamble:
            chunks.append({"title": "Introduction", "text": preamble})
    for i, (pos, _, title) in enumerate(splits):
        end = splits[i + 1][0] if i + 1 < len(splits) else len(text)
        chunks.append({"title": title, "text": text[pos:end].strip()})
    return chunks


def _max_chunk_words(text: str, splits: list[tuple]) -> int:
    positions = [s[0] for s in splits] + [len(text)]
    return max(
        len(text[positions[i] : positions[i + 1]].split())
        for i in range(len(splits))
    )


def _subdivide_on_h3(chunk: dict) -> list[dict]:
    """If chunk has H3 sub-headings, split it on those; otherwise return as-is."""
    body = chunk["text"]
    h3 = list(re.finditer(r"^###\s+(.+?)\s*$", body, re.MULTILINE))
    if len(h3) < 2:
        return [chunk]
    out: list[dict] = []
    if h3[0].start() > 0:
        head = body[: h3[0].start()].strip()
        if head:
            out.append({"title": chunk["title"], "text": head})
    for i, m in enumerate(h3):
        end = h3[i + 1].start() if i + 1 < len(h3) else len(body)
        out.append({"title": m.group(1).strip(), "text": body[m.start() : end].strip()})
    return out


def chunk_article(text: str) -> list[dict]:
    """Split article on H2 headings; subdivide oversized chunks on H3.

    Short articles or those without usable headings return a single chunk.
    """
    text = text.strip()
    word_count = len(text.split())
    headings = [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in _ARTICLE_HEADING.finditer(text)
    ]

    if word_count <= ARTICLE_WORD_THRESHOLD or not headings:
        return [{"title": "", "text": text}]

    h2_splits = [h for h in headings if h[1] == 2]
    if len(h2_splits) < 2:
        # No usable H2 structure — try H2+H3 combined as a fallback.
        mixed = [h for h in headings if h[1] in (2, 3)]
        if len(mixed) < 2:
            return [{"title": "", "text": text}]
        return _build_chunks(text, mixed)

    refined: list[dict] = []
    for chunk in _build_chunks(text, h2_splits):
        if len(chunk["text"].split()) > ARTICLE_MAX_CHUNK_WORDS:
            refined.extend(_subdivide_on_h3(chunk))
        else:
            refined.append(chunk)
    return refined


# ---------- Chunking ----------

def fmt_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_chunk_text(segments: list[dict]) -> str:
    """Join segments, injecting [timestamp] markers every ~30s."""
    out, last_marker = [], -TIMESTAMP_EVERY_S
    for seg in segments:
        if seg["start"] - last_marker >= TIMESTAMP_EVERY_S:
            out.append(f"\n[{fmt_ts(seg['start'])}] ")
            last_marker = seg["start"]
        out.append(seg["text"].strip())
        out.append(" ")
    return "".join(out).strip()


def chunk_transcript(segments: list[dict]) -> list[dict]:
    if not segments:
        return []
    total = segments[-1]["start"] + segments[-1].get("duration", 0)
    window = WINDOW_MINUTES * 60
    overlap = OVERLAP_MINUTES * 60

    if total <= window:
        return [{"start": 0, "end": total, "text": format_chunk_text(segments)}]

    chunks, cursor = [], 0
    while cursor < total:
        end = cursor + window
        segs = [s for s in segments if cursor <= s["start"] < end]
        if segs:
            chunks.append({
                "start": cursor,
                "end": min(end, total),
                "text": format_chunk_text(segs),
            })
        cursor += window - overlap
    return chunks


# ---------- Summarization ----------

CHUNK_PROMPT = """You are summarizing one section of a YouTube video transcript.

Video: {title}
Channel: {channel}
Section: {start} – {end}

Transcript (with [timestamp] markers inline):
---
{text}
---

Produce a Markdown summary of THIS SECTION ONLY:
- 2–3 sentence overview of what's covered
- Key points as bullets, each ending with a [timestamp] reference
- Up to 2 notable short quotes (under 15 words), with timestamps

Preserve timestamps exactly as they appear in the transcript."""


SYNTHESIS_PROMPT = """You are creating a final summary of a YouTube video by synthesizing per-section summaries.

Video: {title}
Channel: {channel}
Duration: {duration}
Published: {published}
Summarized: {summarized}
URL: {url}

Section summaries:
---
{sections}
---

Produce a single Markdown document EXACTLY in this structure:

# {title}

**Channel:** {channel}  
**Duration:** {duration}  
**Published:** {published}  
**Summarized:** {summarized}  
**Source:** {url}

## TL;DR
Three sentences capturing the essence of the video.

## Key Takeaways
5–8 standalone bullets, each insightful or actionable.

## Walkthrough
Section-by-section. Each section: `### [M:SS] Short Title` heading, then 2–4 sentences. Use the timestamps from the section summaries.

## Notable Quotes
Up to 3 short quotes with timestamps. Omit this section entirely if there aren't strong ones.

Preserve all timestamps from the source summaries. Write in clear prose, no filler."""


SINGLE_PASS_PROMPT = """Summarize this YouTube video transcript.

Video: {title}
Channel: {channel}
Duration: {duration}
Published: {published}
Summarized: {summarized}
URL: {url}

Transcript (with [timestamp] markers):
---
{text}
---

Produce a Markdown document EXACTLY in this structure:

# {title}

**Channel:** {channel}  
**Duration:** {duration}  
**Published:** {published}  
**Summarized:** {summarized}  
**Source:** {url}

## TL;DR
Three sentences.

## Key Takeaways
5–8 standalone bullets.

## Walkthrough
Section-by-section with `### [M:SS] Short Title` headings, 2–4 sentences each.

## Notable Quotes
Up to 3 short quotes with timestamps. Omit this section if there aren't strong ones."""


ARTICLE_CHUNK_PROMPT = """You are summarizing one section of a web article.

Article: {title}
Site: {site}
Section heading: {section_title}

Section text (Markdown):
---
{text}
---

Produce a Markdown summary of THIS SECTION ONLY:
- 2–3 sentence overview of what's covered
- Key points as bullets
- Up to 2 short notable quotes (under 20 words), in quotation marks

Be concrete. No filler."""


ARTICLE_SYNTHESIS_PROMPT = """You are creating a final summary of a web article by synthesizing per-section summaries.

Article: {title}
Site: {site}
Author: {author}
Published: {published}
Summarized: {summarized}
URL: {url}

Section summaries:
---
{sections}
---

Produce a single Markdown document EXACTLY in this structure:

# {title}

**Site:** {site}
**Author:** {author}
**Published:** {published}
**Summarized:** {summarized}
**Source:** {url}

## TL;DR
Three sentences capturing the essence of the article.

## Key Takeaways
5–8 standalone bullets, each insightful or actionable.

## Walkthrough
Section-by-section. Use the article's own section headings as `### Heading`, then 2–4 sentences each.

## Notable Quotes
Up to 3 short quotes. Omit this section entirely if there aren't strong ones.

Write in clear prose, no filler."""


ARTICLE_SINGLE_PASS_PROMPT = """Summarize this web article.

Article: {title}
Site: {site}
Author: {author}
Published: {published}
Summarized: {summarized}
URL: {url}

Article text (Markdown):
---
{text}
---

Produce a Markdown document EXACTLY in this structure:

# {title}

**Site:** {site}
**Author:** {author}
**Published:** {published}
**Summarized:** {summarized}
**Source:** {url}

## TL;DR
Three sentences.

## Key Takeaways
5–8 standalone bullets.

## Walkthrough
Section-by-section using the article's own headings as `### Heading`, 2–4 sentences each.

## Notable Quotes
Up to 3 short quotes. Omit this section if there aren't strong ones."""


def call_claude(client: Anthropic, model: str, prompt: str, max_tokens: int) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def summarize(chunks: list[dict], meta: dict, client: Anthropic, model: str) -> str:
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
        print(
            f"  · section {i}/{len(chunks)} "
            f"({fmt_ts(ch['start'])}–{fmt_ts(ch['end'])})",
            file=sys.stderr,
        )
        prompt = CHUNK_PROMPT.format(
            title=meta["title"],
            channel=meta["channel"],
            start=fmt_ts(ch["start"]),
            end=fmt_ts(ch["end"]),
            text=ch["text"],
        )
        section_summaries.append(call_claude(client, model, prompt, max_tokens=1500))

    print("  · synthesizing", file=sys.stderr)
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


def summarize_article(
    chunks: list[dict], meta: dict, client: Anthropic, model: str
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
        print(f"  · section {i}/{len(chunks)}: {label}", file=sys.stderr)
        prompt = ARTICLE_CHUNK_PROMPT.format(
            title=fields["title"],
            site=fields["site"],
            section_title=ch["title"] or "(introduction)",
            text=ch["text"],
        )
        section_summaries.append(call_claude(client, model, prompt, max_tokens=1500))

    print("  · synthesizing", file=sys.stderr)
    prompt = ARTICLE_SYNTHESIS_PROMPT.format(
        sections="\n\n---\n\n".join(section_summaries),
        **fields,
    )
    return call_claude(client, model, prompt, max_tokens=4000)


# ---------- Output ----------

def _normalize_publish_date(s: str | None) -> str | None:
    """Parse a date string and return canonical YYYY-MM-DD, or None if unparseable.

    Handles trafilatura's ISO-like 'YYYY-MM-DD' and yt-dlp's 'YYYYMMDD' shapes.
    """
    if not s:
        return None
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s[:max_len] or "video"


def write_summary(summary: str, meta: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = _normalize_publish_date(meta.get("published")) or datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"{date_str}_{slugify(meta['title'])}.md"
    path.write_text(summary, encoding="utf-8")
    return path


# ---------- Shell completion ----------

BASH_COMPLETION = r"""# bash completion for summarize
# install: source <(./summarize --completion)
_summarize_complete() {
    local cur prev opts models
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    opts="--batch --transcript-file --article --playwright --title --source --out-dir --model --completion --help"
    models="claude-opus-4-7 claude-sonnet-4-6 claude-haiku-4-5-20251001"

    case "$prev" in
        --batch|--transcript-file)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
        --out-dir)
            COMPREPLY=( $(compgen -d -- "$cur") )
            return 0
            ;;
        --model)
            COMPREPLY=( $(compgen -W "$models" -- "$cur") )
            return 0
            ;;
        --title|--source|--article)
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        return 0
    fi
}
complete -F _summarize_complete summarize
complete -F _summarize_complete ./summarize
"""


# ---------- Pipeline ----------

def process_url(url: str, client: Anthropic, model: str, out_dir: Path) -> Path | None:
    print(f"\n→ {url}", file=sys.stderr)
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return None

    print("  · fetching metadata", file=sys.stderr)
    meta = fetch_metadata(url)
    print(f"  · title: {meta['title']}", file=sys.stderr)

    print("  · fetching transcript", file=sys.stderr)
    segments = fetch_transcript(video_id)
    if not segments:
        print("  ✗ no transcript available (captions disabled or absent)", file=sys.stderr)
        return None

    chunks = chunk_transcript(segments)
    print(
        f"  · {len(segments)} segments → {len(chunks)} chunk(s)",
        file=sys.stderr,
    )

    summary = summarize(chunks, meta, client, model)
    path = write_summary(summary, meta, out_dir)
    print(f"  ✓ wrote {path}", file=sys.stderr)
    return path


def process_transcript_file(
    path: Path,
    client: Anthropic,
    model: str,
    out_dir: Path,
    title: str | None,
    source: str | None,
) -> Path | None:
    print(f"\n→ {path}", file=sys.stderr)
    segments = parse_transcript_file(path)
    if not segments:
        print("  ✗ no segments parsed (expected '[MM:SS --> MM:SS] text' lines)", file=sys.stderr)
        return None

    duration = int(segments[-1]["start"] + segments[-1]["duration"])
    meta = {
        "title": title or title_from_filename(path),
        "channel": "Local transcript",
        "duration": duration,
        "published": None,
        "url": source or "",
    }
    print(f"  · title: {meta['title']}", file=sys.stderr)

    chunks = chunk_transcript(segments)
    print(f"  · {len(segments)} segments → {len(chunks)} chunk(s)", file=sys.stderr)

    summary = summarize(chunks, meta, client, model)
    path_out = write_summary(summary, meta, out_dir)
    print(f"  ✓ wrote {path_out}", file=sys.stderr)
    return path_out


def process_article(
    url: str, client: Anthropic, model: str, out_dir: Path,
    *, force_playwright: bool = False,
) -> Path | None:
    print(f"\n→ {url}", file=sys.stderr)
    print("  · fetching article", file=sys.stderr)
    try:
        meta, text = fetch_article(url, force_playwright=force_playwright)
    except RuntimeError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return None

    print(f"  · title: {meta['title']}", file=sys.stderr)
    print(f"  · {meta['word_count']} words", file=sys.stderr)

    chunks = chunk_article(text)
    print(f"  · {len(chunks)} chunk(s)", file=sys.stderr)

    summary = summarize_article(chunks, meta, client, model)
    path = write_summary(summary, meta, out_dir)
    print(f"  ✓ wrote {path}", file=sys.stderr)
    return path


def main() -> None:
    if "--completion" in sys.argv[1:]:
        print(BASH_COMPLETION)
        return

    parser = argparse.ArgumentParser(description="Summarize YouTube videos and web articles with Claude.")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("url", nargs="?", help="YouTube URL")
    src.add_argument("--batch", metavar="FILE", help="File with one URL per line")
    src.add_argument("--transcript-file", metavar="FILE", help="Local whisper-style transcript to summarize")
    src.add_argument("--article", metavar="URL", help="Web article URL to fetch and summarize")
    parser.add_argument("--title", help="Override title (transcript-file mode; default: inferred from filename)")
    parser.add_argument("--source", help="Optional source/URL to cite in the output header (transcript-file mode)")
    parser.add_argument("--out-dir", default="./summaries", help="Output dir (default: ./summaries)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    parser.add_argument("--completion", action="store_true", help="Print bash completion script and exit")
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Force Playwright (Chromium) for article fetch (default: auto-fallback on failure; only meaningful with --article)",
    )
    args = parser.parse_args()

    if args.playwright and not args.article:
        parser.error("--playwright requires --article")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = Anthropic(max_retries=API_MAX_RETRIES)
    out_dir = Path(args.out_dir)

    if args.batch:
        with open(args.batch) as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        print(f"processing {len(urls)} URL(s)", file=sys.stderr)
        for url in urls:
            try:
                process_url(url, client, args.model, out_dir)
            except Exception as e:
                print(f"  ✗ error: {e}", file=sys.stderr)
    elif args.transcript_file:
        process_transcript_file(
            Path(args.transcript_file), client, args.model, out_dir,
            title=args.title, source=args.source,
        )
    elif args.article:
        process_article(args.article, client, args.model, out_dir, force_playwright=args.playwright)
    else:
        process_url(args.url, client, args.model, out_dir)


if __name__ == "__main__":
    main()

