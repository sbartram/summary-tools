# Publish Date in Filename + `**Summarized:**` Header Field — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use the article publish date (or YouTube upload date) as the filename's leading date, falling back to today's date when unavailable; add a `**Summarized:**` line to output headers showing when the summary was produced.

**Architecture:** A new `_normalize_publish_date()` helper parses both trafilatura's `YYYY-MM-DD` and yt-dlp's `YYYYMMDD` shapes into a canonical form. `fetch_metadata` (YouTube) starts populating `meta["published"]`. `write_summary` uses that field for the filename (with today's date fallback). Prompt templates gain a `**Summarized:**` line (and YouTube/transcript templates also gain `**Published:**`, since their prompts didn't render the field before). All callers of `summarize()` thread `published` and `summarized` through the format() calls.

**Tech Stack:** Python 3.10+, existing `trafilatura` and `yt_dlp` deps only. No new packages.

**Spec:** `docs/superpowers/specs/2026-05-15-publish-date-filename-design.md`

**Spec adjustment:** The spec said "transcript-file mode's prompts are not changed." Since transcript-file mode shares `SYNTHESIS_PROMPT` / `SINGLE_PASS_PROMPT` with the YouTube pipeline, updating those prompts necessarily affects transcript-file output. We accept this: transcript-file summaries will gain `**Published:** —` (no source date) and `**Summarized:** <today>` lines. This is consistent with the feature's intent (record when the summary was made) and avoids prompt duplication.

**Testing note:** This codebase has no unit test framework. Verification is manual smoke testing per the project's testing bar. Each task ends with a concrete shell command and expected output.

---

## File map

- Modify: `summarize.py`
  - Add `_normalize_publish_date` helper (Task 1).
  - Add `published` to `fetch_metadata()` return dict (Task 1).
  - Update `write_summary()` to choose filename date (Task 2).
  - Update `ARTICLE_SYNTHESIS_PROMPT` and `ARTICLE_SINGLE_PASS_PROMPT` to include `{summarized}` field (Task 3).
  - Update `summarize_article()` to pass `summarized` (Task 3).
  - Update `SYNTHESIS_PROMPT` and `SINGLE_PASS_PROMPT` to include `{published}` and `{summarized}` fields (Task 4).
  - Update `summarize()` and `process_transcript_file()` to pass `published` and `summarized` (Task 4).

That's it — one file.

---

## Task 1: Add `_normalize_publish_date` helper and YouTube `published` field

**Why first:** Pure-data change that adds a new helper and a new dict key without anyone reading it yet. Risk-free, easy to verify in isolation.

**Files:**
- Modify: `summarize.py` (helper near `slugify`, around line 652).
- Modify: `summarize.py:68-77` (`fetch_metadata`).

- [ ] **Step 1: Add the helper**

Add this function in `summarize.py` immediately above the existing `slugify` function (around line 652):

```python
def _normalize_publish_date(s: str | None) -> str | None:
    """Parse a date string and return canonical YYYY-MM-DD, or None if unparseable.

    Handles trafilatura's ISO-like 'YYYY-MM-DD' and yt-dlp's 'YYYYMMDD' shapes.
    """
    if not s:
        return None
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None
```

(`re` is already imported at module top.)

- [ ] **Step 2: Add `published` to `fetch_metadata` return dict**

In `summarize.py`, change the return dict of `fetch_metadata()` from:

```python
    return {
        "title": info.get("title", "Untitled"),
        "channel": info.get("uploader", "Unknown"),
        "duration": int(info.get("duration") or 0),
        "url": url,
    }
```

to:

```python
    return {
        "title": info.get("title", "Untitled"),
        "channel": info.get("uploader", "Unknown"),
        "duration": int(info.get("duration") or 0),
        "published": _normalize_publish_date(info.get("upload_date")),
        "url": url,
    }
```

- [ ] **Step 3: Smoke-verify the helper directly**

```bash
UV_TOOL_PY="$(uv tool dir)/summary-tools/bin/python"
"$UV_TOOL_PY" -c "
import sys
sys.path.insert(0, '/Volumes/data2/scottb/dev/bartram/summary-tools')
from summarize import _normalize_publish_date
assert _normalize_publish_date('2024-12-31') == '2024-12-31', 'iso failed'
assert _normalize_publish_date('20241231') == '2024-12-31', 'yt-dlp failed'
assert _normalize_publish_date('2024-12-31T10:30:00Z') == '2024-12-31', 'iso-with-time failed'
assert _normalize_publish_date(None) is None, 'None failed'
assert _normalize_publish_date('') is None, 'empty failed'
assert _normalize_publish_date('not a date') is None, 'garbage failed'
print('OK')
"
```

Expected: prints `OK`.

(Note: this uses `python` directly to test the helper without touching the network or the LLM — no API cost.)

- [ ] **Step 4: Smoke-verify `fetch_metadata` populates `published`**

```bash
"$UV_TOOL_PY" -c "
import sys
sys.path.insert(0, '/Volumes/data2/scottb/dev/bartram/summary-tools')
from summarize import fetch_metadata
m = fetch_metadata('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
print('published:', m['published'])
assert m['published'] is not None and len(m['published']) == 10, f'bad published: {m[\"published\"]}'
print('OK')
"
```

Expected: prints `published: 2009-10-25` (or similar real date for the Rick Astley video) then `OK`. The exact date doesn't matter — just that it's a parseable `YYYY-MM-DD` and not `None`.

If yt-dlp blocks the request (cloud IP / rate limit), try a different stable YouTube URL — the test is just confirming `upload_date` flows through.

- [ ] **Step 5: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
add _normalize_publish_date helper and surface YouTube upload date

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Use publish date in summary filename

**Files:**
- Modify: `summarize.py:658-663` (`write_summary`).

- [ ] **Step 1: Change the filename-date logic**

In `summarize.py`, change `write_summary` from:

```python
def write_summary(summary: str, meta: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"{date_str}_{slugify(meta['title'])}.md"
    path.write_text(summary, encoding="utf-8")
    return path
```

to:

```python
def write_summary(summary: str, meta: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = _normalize_publish_date(meta.get("published")) or datetime.now().strftime("%Y-%m-%d")
    path = out_dir / f"{date_str}_{slugify(meta['title'])}.md"
    path.write_text(summary, encoding="utf-8")
    return path
```

(`_normalize_publish_date` was added in Task 1.)

- [ ] **Step 2: Smoke-verify with an article URL whose `published` is known**

```bash
uv tool install --force '.[playwright]'  # if not already current
# Use a recently-summarized article to keep cost down (you've already paid the
# API call for this one; the filename change is the only new behavior under test):
summarize --article 'https://x.com/garrytan/status/2046876981711769720' --playwright
```

Expected: a new summary file is written. Its filename now starts with the article's published date (2026-04-21 for that Garry Tan X.com post), NOT today's date. Compare to the previous run from this session — same content but different filename prefix.

The header inside the file should still show `**Published:** 2026-04-21` (unchanged from before — that field already existed for articles).

If you don't want to spend another API call: code-review is acceptable for this task since the change is a one-line substitution and Task 1's smoke test already verified `_normalize_publish_date` works.

- [ ] **Step 3: Verify the fallback path**

A YouTube URL whose metadata fetched correctly in Task 1 will now produce filenames using the YouTube upload date (since Task 1 set `meta["published"]`). To test the fallback, code-review:

- The `or` in `date_str = _normalize_publish_date(...) or datetime.now()...` ensures that when `meta.get("published")` is missing or returns `None` from the helper, today's date is used.
- `--transcript-file` mode's meta dict has no `published` key, so `meta.get("published")` returns `None`, triggering the fallback. Existing behavior preserved.

No runtime verification needed for the fallback if Task 1's helper-test coverage is satisfactory.

- [ ] **Step 4: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
use source publish date in summary filename when available

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Article prompts — add `**Summarized:**` field

**Why before Task 4:** Article path already has `Published` working; just adding `Summarized`. YouTube/transcript path needs both `Published` AND `Summarized` and touches more callers. Doing articles first keeps each diff focused.

**Files:**
- Modify: `summarize.py:497-531` (`ARTICLE_SYNTHESIS_PROMPT`).
- Modify: `summarize.py:534-566` (`ARTICLE_SINGLE_PASS_PROMPT`).
- Modify: `summarize.py:616-647` (`summarize_article`).

- [ ] **Step 1: Update `ARTICLE_SYNTHESIS_PROMPT`**

In `summarize.py`, change the context block of `ARTICLE_SYNTHESIS_PROMPT` from:

```
Article: {title}
Site: {site}
Author: {author}
Published: {published}
URL: {url}
```

to:

```
Article: {title}
Site: {site}
Author: {author}
Published: {published}
Summarized: {summarized}
URL: {url}
```

And change the output-structure block from:

```
**Site:** {site}
**Author:** {author}
**Published:** {published}
**Source:** {url}
```

to:

```
**Site:** {site}
**Author:** {author}
**Published:** {published}
**Summarized:** {summarized}
**Source:** {url}
```

- [ ] **Step 2: Update `ARTICLE_SINGLE_PASS_PROMPT`**

In `summarize.py`, change the context block of `ARTICLE_SINGLE_PASS_PROMPT` from:

```
Article: {title}
Site: {site}
Author: {author}
Published: {published}
URL: {url}
```

to:

```
Article: {title}
Site: {site}
Author: {author}
Published: {published}
Summarized: {summarized}
URL: {url}
```

And change the output-structure block from:

```
**Site:** {site}
**Author:** {author}
**Published:** {published}
**Source:** {url}
```

to:

```
**Site:** {site}
**Author:** {author}
**Published:** {published}
**Summarized:** {summarized}
**Source:** {url}
```

- [ ] **Step 3: Thread `summarized` through `summarize_article`**

In `summarize.py`, change the `fields` dict at the top of `summarize_article` from:

```python
    fields = {
        "title": meta["title"],
        "site": meta.get("site") or "—",
        "author": meta.get("author") or "—",
        "published": meta.get("published") or "—",
        "url": meta["url"],
    }
```

to:

```python
    fields = {
        "title": meta["title"],
        "site": meta.get("site") or "—",
        "author": meta.get("author") or "—",
        "published": meta.get("published") or "—",
        "summarized": datetime.now().strftime("%Y-%m-%d"),
        "url": meta["url"],
    }
```

`datetime` is already imported at module top. The rest of `summarize_article` (which uses `**fields` for the format() calls) needs no change — the new key flows through automatically.

- [ ] **Step 4: Smoke-verify the article header now shows `**Summarized:**`**

```bash
summarize --article 'https://x.com/garrytan/status/2046876981711769720' --playwright
```

Expected: the output file's header now contains a `**Summarized:** 2026-05-15` line (or whatever today is) immediately after the `**Published:**` line, before the `**Source:**` line. The filename should still start with `2026-04-21_` (publish date from Task 2).

If you've already used your "free" article test in Task 2 and want to skip this one to save cost, code-review the prompt diff to confirm the new line is in the output structure block.

- [ ] **Step 5: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
add Summarized header field to article summaries

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: YouTube/transcript prompts — add `**Published:**` and `**Summarized:**`

**Files:**
- Modify: `summarize.py:410-442` (`SYNTHESIS_PROMPT`).
- Modify: `summarize.py:445-476` (`SINGLE_PASS_PROMPT`).
- Modify: `summarize.py:578-613` (`summarize()` body).
- Modify: `summarize.py:738-767` (`process_transcript_file`).

- [ ] **Step 1: Update `SYNTHESIS_PROMPT`**

In `summarize.py`, change the context block of `SYNTHESIS_PROMPT` from:

```
Video: {title}
Channel: {channel}
Duration: {duration}
URL: {url}
```

to:

```
Video: {title}
Channel: {channel}
Duration: {duration}
Published: {published}
Summarized: {summarized}
URL: {url}
```

And change the output-structure block from:

```
**Channel:** {channel}  
**Duration:** {duration}  
**Source:** {url}
```

to:

```
**Channel:** {channel}  
**Duration:** {duration}  
**Published:** {published}  
**Summarized:** {summarized}  
**Source:** {url}
```

(Keep the trailing two-space line breaks — they're part of the existing template.)

- [ ] **Step 2: Update `SINGLE_PASS_PROMPT`**

In `summarize.py`, change the context block of `SINGLE_PASS_PROMPT` from:

```
Video: {title}
Channel: {channel}
Duration: {duration}
URL: {url}
```

to:

```
Video: {title}
Channel: {channel}
Duration: {duration}
Published: {published}
Summarized: {summarized}
URL: {url}
```

And change the output-structure block from:

```
**Channel:** {channel}  
**Duration:** {duration}  
**Source:** {url}
```

to:

```
**Channel:** {channel}  
**Duration:** {duration}  
**Published:** {published}  
**Summarized:** {summarized}  
**Source:** {url}
```

(Keep the trailing two-space line breaks — they're part of the existing template.)

- [ ] **Step 3: Thread `published` and `summarized` through `summarize()`**

In `summarize.py`, change the `SINGLE_PASS_PROMPT.format(...)` call inside `summarize()` from:

```python
        prompt = SINGLE_PASS_PROMPT.format(
            title=meta["title"],
            channel=meta["channel"],
            duration=fmt_ts(meta["duration"]),
            url=meta["url"],
            text=chunks[0]["text"],
        )
```

to:

```python
        prompt = SINGLE_PASS_PROMPT.format(
            title=meta["title"],
            channel=meta["channel"],
            duration=fmt_ts(meta["duration"]),
            published=meta.get("published") or "—",
            summarized=datetime.now().strftime("%Y-%m-%d"),
            url=meta["url"],
            text=chunks[0]["text"],
        )
```

And change the `SYNTHESIS_PROMPT.format(...)` call further down in the same function from:

```python
    prompt = SYNTHESIS_PROMPT.format(
        title=meta["title"],
        channel=meta["channel"],
        duration=fmt_ts(meta["duration"]),
        url=meta["url"],
        sections="\n\n---\n\n".join(section_summaries),
    )
```

to:

```python
    prompt = SYNTHESIS_PROMPT.format(
        title=meta["title"],
        channel=meta["channel"],
        duration=fmt_ts(meta["duration"]),
        published=meta.get("published") or "—",
        summarized=datetime.now().strftime("%Y-%m-%d"),
        url=meta["url"],
        sections="\n\n---\n\n".join(section_summaries),
    )
```

(`CHUNK_PROMPT` doesn't render headers, so it's unchanged.)

- [ ] **Step 4: Update `process_transcript_file` meta dict**

In `summarize.py`, change the `meta = {...}` dict in `process_transcript_file` from:

```python
    meta = {
        "title": title or title_from_filename(path),
        "channel": "Local transcript",
        "duration": duration,
        "url": source or "",
    }
```

to:

```python
    meta = {
        "title": title or title_from_filename(path),
        "channel": "Local transcript",
        "duration": duration,
        "published": None,
        "url": source or "",
    }
```

The `meta.get("published") or "—"` in Task 4 Step 3 will resolve this to `"—"` in the rendered template. `write_summary` (Task 2) already uses today's date when `published` is `None`.

- [ ] **Step 5: Smoke-verify a YouTube video header now has both `**Published:**` and `**Summarized:**`**

```bash
summarize https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Expected:
- Filename starts with the video's upload date (e.g., `2009-10-25_...`).
- Output header contains both `**Published:** 2009-10-25` and `**Summarized:** 2026-05-15` lines.
- `**Channel:**`, `**Duration:**`, and `**Source:**` lines still present.

If you'd rather not spend the API call for a real YouTube run: code-review the prompt and format() diffs and skip the live test.

- [ ] **Step 6: Code-review the transcript-file change**

Manual verification of transcript-file mode requires a `--transcript-file FILE` and `--title`/`--source` setup. To avoid that, confirm by code review that:

- `process_transcript_file`'s `meta` dict now has `published: None` (Step 4).
- `summarize()` reads `meta.get("published") or "—"` (Step 3).
- `write_summary` (Task 2) falls back to today's date when `published` is `None`.

Therefore: transcript-file summaries will get `**Published:** —` and `**Summarized:** <today>` in their headers, and their filename will use today's date. This is the spec-adjustment behavior described in the plan header.

- [ ] **Step 7: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
add Published and Summarized header fields to video summaries

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: End-to-end verification

A final pass through the spec's test plan.

- [ ] **Step 1: Article with publish date**

```bash
summarize --article 'https://x.com/garrytan/status/2046876981711769720' --playwright
```

Expected: filename starts with `2026-04-21_`. Header has both `**Published:** 2026-04-21` and `**Summarized:** 2026-05-15`.

- [ ] **Step 2: YouTube with upload date**

```bash
summarize https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Expected: filename starts with the video's upload date. Header has new `**Published:**` and `**Summarized:**` lines.

(Skip if previous task verifications already covered these scenarios.)

- [ ] **Step 3: Article with no parseable publish date — code-review only**

Confirm by inspecting `write_summary` that when `meta.get("published")` is `None` or unparseable, `date_str` falls back to `datetime.now().strftime("%Y-%m-%d")`. The behavior is:
- Filename uses today's date.
- Header `**Published:** —` (from `meta.get("published") or "—"` in summarize_article).
- Header `**Summarized:** <today>`.

- [ ] **Step 4: Final commit hygiene**

```bash
git log --oneline -6
git status
```

Expected: four new commits (Tasks 1–4) plus this final check. Working tree clean. Commit messages all lowercase imperative, no `feat:` prefix.

---

## Summary

After completion, the feature shape is:

- **Filename leading date** uses article publish date (trafilatura) or YouTube upload date (yt-dlp), with today's date fallback when missing or unparseable.
- **Output headers** all carry a new `**Summarized:** <today>` line.
- **YouTube/transcript output headers** also carry a new `**Published:** <date-or-em-dash>` line (previously absent).
- **No CLI changes.** Behavior is automatic.
- **No dependency changes.**
