# Playwright Fallback for JS-Rendered Articles

**Date:** 2026-05-15
**Status:** Approved for planning

## Problem

`--article` mode uses `trafilatura` to fetch and extract web article content. trafilatura performs a plain HTTP request and parses the returned HTML, which fails for sites that render their body via client-side JavaScript:

- **X.com long-form posts** — JS-only SPA; `trafilatura` returns no content.
- **Morningstar Q&A pages** — partial extraction; the visible article body lives in a DOM subtree built after page load.

The existing browser-UA workaround in `fetch_article()` (lines 149-154) handles HTTP-202 bot challenges, but cannot solve the rendering problem. The current CLAUDE.md gotchas note Playwright as the only working path for these sites.

## Goals

1. Add a Playwright-based fetch path that handles JS-rendered articles.
2. Trigger it both explicitly (user-requested) and automatically (when the default path hard-fails).
3. Keep Playwright optional — the base install must not require it.
4. Preserve the `(meta, markdown_text)` interface so `chunk_article` and `summarize_article` are untouched.

## Non-Goals

- Whisper fallback for caption-less videos (separate backlog item).
- Auto-fallback on partial/short extractions — only on hard `RuntimeError`.
- Auto-installing the Chromium binary.
- Replacing trafilatura on the happy path.

## Design

### Architecture

The Playwright fallback is a **rendering-layer swap-in**, not a new extraction pipeline. trafilatura's `extract()` already accepts an HTML string; we substitute the source of that HTML.

```
Default path:    trafilatura.fetch_url(url) ──► HTML ──► trafilatura.extract(html) ──► (meta, markdown)
Playwright path: chromium.goto(url)         ──► HTML ──► trafilatura.extract(html) ──► (meta, markdown)
```

`chunk_article` and `summarize_article` are unmodified — both fetch paths return identical shapes.

### Components

**`fetch_article_playwright(url: str) -> tuple[dict, str]`** — new function in `summarize.py`.

- Lazy-imports `playwright.sync_api` inside the function. Raises `RuntimeError("playwright not installed. Run: uv tool install '.[playwright]' && playwright install chromium")` on `ImportError`.
- Launches headless Chromium via `sync_playwright()` context manager.
- Navigates with `page.goto(url, wait_until="networkidle")` to allow client-side rendering.
- Grabs `page.content()` (fully rendered HTML).
- Hands the HTML string to `trafilatura.extract(html, output_format="markdown", include_comments=False, include_tables=True)` and `trafilatura.extract_metadata(html)`.
- Raises `RuntimeError(f"no article content extracted from {url} (after Playwright)")` if extraction yields empty text.

**`_meta_from_trafilatura(md, url: str, text: str) -> dict`** — extracted helper.

Both `fetch_article` and `fetch_article_playwright` build the same `{title, author, site, published, url, word_count}` dict from a trafilatura metadata object. The helper deduplicates this construction.

**`fetch_article(url: str, *, force_playwright: bool = False) -> tuple[dict, str]`** — modified dispatcher.

- If `force_playwright`: call `fetch_article_playwright()` directly.
- Otherwise: try existing trafilatura HTTP path. On any `RuntimeError`:
  - Attempt `fetch_article_playwright()`.
  - If Playwright is missing (`ImportError` caught from the lazy import), re-raise the *original* trafilatura error with `" (install '[playwright]' extra to enable fallback for JS-rendered sites)"` appended.

### CLI

Add `--playwright` boolean flag to the argument parser. Only meaningful alongside `--article`; argparse doesn't need to enforce that (silent no-op for non-article modes is acceptable).

Thread through `process_article(url, client, model, out_dir, force_playwright=False)`.

Bash completion (`--completion` output) gains `--playwright` in the option list.

### Packaging

`pyproject.toml`:
```toml
[project.optional-dependencies]
transcribe = ["pywhispercpp"]
playwright = ["playwright"]
```

`requirements.txt`: append `playwright` for the dev-venv path (matching `pywhispercpp`'s precedent).

### Error Handling Matrix

| Condition | Behavior |
|---|---|
| `--playwright` flag, `playwright` package missing | `RuntimeError("playwright not installed. Run: uv tool install '.[playwright]' && playwright install chromium")` |
| `--playwright` flag, Chromium binary missing | Catch Playwright's launch error, re-raise with `"Run: playwright install chromium"` |
| Auto-fallback path, `playwright` package missing | Re-raise *original* trafilatura `RuntimeError` with `" (install '[playwright]' extra to enable fallback for JS-rendered sites)"` appended |
| Playwright runs, navigation times out | Propagate Playwright's `TimeoutError` |
| Playwright runs, trafilatura extracts empty text | `RuntimeError(f"no article content extracted from {url} (after Playwright)")` |

### Documentation Updates

**README.md** — add a section after the `[transcribe]` install instructions:

> For JS-rendered articles (X.com long-form, Morningstar Q&A, similar):
> ```
> uv tool install '.[playwright]'
> playwright install chromium
> ```
> Then either run with `--playwright` to force, or let auto-fallback trigger when the default fetcher fails.

**CLAUDE.md** changes:
1. Remove the "Playwright fallback for JS-rendered articles" bullet from "Possible future work".
2. Update the X.com gotcha to note that `--playwright` is the supported path.
3. Add a one-paragraph note in "Architecture" describing `fetch_article`'s dispatch logic (try trafilatura → auto-fallback to Playwright on `RuntimeError` → `--playwright` forces Playwright from the start).

## Test Plan

Manual verification on representative URLs:

1. **trafilatura happy path unaffected** — summarize a known-good blog post (e.g., a Substack article) without `--playwright`; confirm no Playwright code executes.
2. **`--playwright` flag forces** — same blog post with `--playwright`; confirm Chromium launches and result matches.
3. **Auto-fallback on hard failure** — Morningstar URL without `--playwright`; trafilatura fails, Playwright takes over, result matches what a manual run produces.
4. **Forced Playwright on Morningstar** — same URL with `--playwright`; confirm fast path (no trafilatura attempt first).
5. **Missing extra, auto-fallback** — uninstall the `[playwright]` extra; run a Morningstar URL; confirm error message includes the install hint.
6. **Missing extra, forced** — same; run with `--playwright`; confirm clear install error.

No unit tests in this codebase currently. Manual smoke tests are the bar.

## Risks

- **Chromium install friction** — users will hit `playwright install chromium` confusion. Mitigated by README and the runtime error message.
- **Headless detection** — some sites detect headless Chromium and serve different content. Out of scope; if it bites, a future bullet can add stealth-plugin or non-headless modes.
- **`networkidle` timeouts** — some sites never reach network idle (long-polling, ads). 30s default timeout means worst-case failure is slow. Acceptable for now; can revisit if it becomes painful.

## Out of Scope (Possible Follow-ups)

- Stealth/anti-detection plugins for sites that block headless browsers.
- Configurable wait strategy (`domcontentloaded` vs `networkidle` vs custom selectors).
- Caching rendered HTML to disk to avoid repeat Chromium launches on retries.
- Auto-fallback heuristics beyond hard failure (e.g., short-text detection).
