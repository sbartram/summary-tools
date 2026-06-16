# Retain Raw Reference Material

## Goal

Persist the raw source material — the transcript for videos, the extracted
body for articles — alongside every summary, so both can be imported into an
llm-wiki as paired reference material.

Today only the summary `.md` is written. The pipeline already holds the raw
material in memory at the moment it summarizes, so this is a persistence
change, not a new fetch.

## Approach

Write the raw material in the `process_*` orchestration layer, right next to
the existing `write_summary` call. All disk I/O stays in that layer; the
`fetch_*` and `summarize*` stages remain pure. No new network calls, no
re-fetching — we persist exactly what was summarized.

Rejected alternatives:
- A separate `--export-raw` pass that re-fetches: wasteful, and can drift from
  what was actually summarized.
- Writing raw inside `summarize()`/`fetch_*`: spreads file I/O across stages
  that are currently pure.

## File layout

Sidecar files share the summary's `YYYY-MM-DD_<slug>` stem, in the same
`summaries/` directory:

| Mode | Summary | Raw sidecar |
|------|---------|-------------|
| video / transcript-file | `2026-03-31_slug.md` | `2026-03-31_slug.transcript.txt` |
| article | `2026-03-31_slug.md` | `2026-03-31_slug.source.md` |

Pairing by filename stem keeps wiki import trivial.

## Components

### 1. `SummaryResult` gains two optional fields (`summarize.py`)

```python
raw: str | None = None        # the retained reference text
raw_path: Path | None = None  # where it was written
```

Optional and defaulted, so existing construction sites are unaffected.

### 2. Helpers

- `transcript_to_text(segments) -> str` — emits one faithful line per segment,
  `[{start} --> {end}]  {text}`, using `fmt_ts`. `fmt_ts` output (`M:SS` /
  `H:MM:SS`) matches the `_WHISPER_LINE` regex, so the file round-trips back
  through `--transcript-file`.
- `write_sidecar(text, summary_path, suffix) -> Path` — writes
  `summary_path.with_name(summary_path.stem + suffix)`.

### 3. Wire into the three `process_*` functions

After `write_summary`:

- `process_url`: `transcript_to_text(segments)` → `.transcript.txt`.
- `process_transcript_file`: `transcript_to_text(segments)` → `.transcript.txt`.
- `process_article`: `text` (already markdown) → `.source.md`.

Each sets `result.raw` and `result.raw_path`.

### 4. Web service (`server.py`)

`_job_to_dict` adds `result["raw"]` and `result["raw_path"]`, gated behind the
existing `include_markdown` flag so the list endpoint stays lightweight.
`GET /jobs/{id}` then returns reference material to API callers.

## Behavior & edge cases

- **Always on**, no flag.
- A caption-less video returns `None` before any summary is written, so no
  sidecar is produced. Correct.
- Article raw is the boilerplate-stripped markdown body (what trafilatura or
  Playwright extracted), not the original HTML — the clean reference material
  wanted in a wiki.

## Testing

Manual smoke verification, per repo convention:

1. `summarize <youtube-url>` → confirm `…_slug.md` and `…_slug.transcript.txt`
   both appear; re-run `summarize --transcript-file …_slug.transcript.txt` to
   confirm the sidecar round-trips.
2. `summarize --article <url>` → confirm `…_slug.md` and `…_slug.source.md`.

## Docs

Update CLAUDE.md (architecture, stage 5 note, web-service note) and README.
