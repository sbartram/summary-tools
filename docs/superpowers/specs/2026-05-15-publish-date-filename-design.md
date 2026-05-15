# Publish Date in Filename + `**Summarized:**` Header Field

**Date:** 2026-05-15
**Status:** Approved for planning

## Problem

The output summary file currently uses today's date in its filename (`YYYY-MM-DD_<slug>.md`) and the in-file header records source metadata (Site, Author, Published for articles; Channel, Duration for videos) but does not record when the summary itself was produced.

This makes it hard to:
- Find the summary of a specific article by source date (e.g., "the post Karpathy wrote on 2024-12-31") — the filename reflects when *I* summarized it, not when it was published.
- Distinguish original publish date from summarize date when both are useful (e.g., "I summarized a 2-year-old article last week").

## Goals

1. Filenames use the **source publish date** (article published date or YouTube upload date), falling back to today's date when unavailable.
2. Output headers gain a `**Summarized:**` field showing the date the summary was produced.
3. YouTube summaries also gain a `**Published:**` field (currently missing) so the publish date is visible in the file body, not only inferable from the filename.
4. No CLI surface changes; behavior is automatic.

## Non-Goals

- Transcript-file mode getting a `--published` flag (no source publish date is available).
- Time-of-day precision in either field — date only.
- Rewriting existing summaries on disk.

## Design

### Date sources

| Mode | `meta["published"]` source | Format pre-normalize |
|---|---|---|
| `--article` | `trafilatura.extract_metadata().date` (existing) | typically `YYYY-MM-DD` |
| YouTube (`url`, `--batch`) | `yt_dlp` `info["upload_date"]` (new) | `YYYYMMDD` |
| `--transcript-file` | not set | — |

### Components

**`_normalize_publish_date(s: str | None) -> str | None`** — new helper. Parses the first `YYYY-?MM-?DD` substring; returns `YYYY-MM-DD` or `None` if no match. Lives in `summarize.py` near `slugify`.

**`fetch_metadata(url)`** (YouTube path) — add `"published": _normalize_publish_date(info.get("upload_date"))` to the returned dict.

**`write_summary(summary, meta, out_dir)`** — choose filename date as:

```python
date_str = _normalize_publish_date(meta.get("published")) or datetime.now().strftime("%Y-%m-%d")
```

No other behavior change.

**Prompt templates** — pass `summarized=datetime.now().strftime("%Y-%m-%d")` into all four format() calls (single-pass and synthesis × YouTube and article). Add a `Summarized: {summarized}` line to the context block and a `**Summarized:** {summarized}` line to the output-structure block of each template. YouTube prompts also gain `Published: {published}` (context) and `**Published:** {published}` (output) — the field exists in meta but isn't currently rendered.

**Summarize callers** — `summarize()` (YouTube) gains `published` and `summarized` field threading similar to how `summarize_article()` already threads its fields. `process_transcript_file` continues to not have `published`; its prompts are not changed.

### Header placement

The `**Summarized:**` field is placed immediately after `**Published:**`:

```
**Site:** X (formerly Twitter)
**Author:** —
**Published:** 2026-04-21
**Summarized:** 2026-05-15
**Source:** https://x.com/...
```

For YouTube (after this change):

```
**Channel:** ...
**Duration:** ...
**Published:** 2024-12-31
**Summarized:** 2026-05-15
**Source:** https://youtube.com/...
```

### Fallback behavior

- `meta["published"]` is `None` or `_normalize_publish_date` returns `None` → filename uses today's date.
- Header `**Published:**` still renders as `—` (existing behavior; trafilatura/yt-dlp returned nothing).
- `**Summarized:**` is always today's date — never `—`.

## Test Plan

Manual smoke verification per the project's testing bar:

1. **Article with publish date** — summarize a known-dated article (e.g., the X.com post from earlier dated 2026-04-21). Verify filename starts with `2026-04-21_`, header has both `**Published:** 2026-04-21` and `**Summarized:** 2026-05-15`.
2. **YouTube with upload date** — summarize a YouTube video. Verify filename uses the video's upload date and the header gains a `**Published:**` line.
3. **Article with no publish date** — summarize an article where trafilatura can't extract a date. Verify filename falls back to today's date and header has `**Published:** —` plus `**Summarized:** <today>`.
4. **Transcript-file mode** — summarize a local transcript. Verify filename uses today's date and the header is unchanged from current behavior (no Published, no Summarized — out of scope for this change).

## Risks

- **YouTube prompt change is a content change**, not just a header rearrangement — adding a `Published:` line to the SYNTHESIS / SINGLE_PASS templates and `Summarized:` to all four templates is the actual scope. Past summaries will look slightly different from future ones; acceptable.
- **trafilatura publish dates are not always reliable** — some sites return wrong dates or future dates. Out of scope; we trust whatever trafilatura returns. The fallback handles the missing case, not the wrong case.

## Out of Scope (Possible Follow-ups)

- `--published YYYY-MM-DD` CLI override for transcript-file mode.
- Smarter publish-date parsing (full ISO 8601, RFC 822) — current scope is just `YYYY-MM-DD` / `YYYYMMDD`.
- Backfill: rewriting existing summaries' filenames or headers.
