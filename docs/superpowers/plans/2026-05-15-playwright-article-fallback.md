# Playwright Fallback for JS-Rendered Articles — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Playwright-based article-fetch path that handles JS-rendered sites (X.com long-form, Morningstar Q&A), with both an explicit `--playwright` flag and auto-fallback when the default trafilatura path hard-fails.

**Architecture:** Playwright is a *rendering-layer swap-in*, not a new extraction pipeline. Render the page with headless Chromium, grab `page.content()`, and feed that HTML to `trafilatura.extract()` — so boilerplate stripping, Markdown conversion, and metadata extraction stay in one place. `chunk_article` and downstream stages are untouched.

**Tech Stack:** Python 3.10+, Playwright (Chromium, sync API), trafilatura (existing).

**Testing note:** This codebase has no unit test framework. Verification is manual smoke testing per the spec. Each task ends with a concrete shell command and expected output.

**Spec:** `docs/superpowers/specs/2026-05-15-playwright-article-fallback-design.md`

---

## File map

- Modify: `summarize.py` — extract `_meta_from_trafilatura` helper, add `fetch_article_playwright()`, refactor `fetch_article()` into a dispatcher + `_fetch_article_trafilatura()`, add `--playwright` flag, update bash completion.
- Modify: `pyproject.toml` — add `playwright` to `[project.optional-dependencies]`.
- Modify: `requirements.txt` — append `playwright`.
- Modify: `README.md` — add a "JS-rendered articles" install section after the `[transcribe]` block.
- Modify: `CLAUDE.md` — remove the "Playwright fallback" future-work bullet, update the X.com gotcha, add a dispatch note in Architecture.

---

## Task 1: Extract `_meta_from_trafilatura` helper (pure refactor)

**Why first:** Future tasks need to reuse this. Doing it first keeps the diff to `fetch_article` smaller and the change is risk-free (no behavior change).

**Files:**
- Modify: `summarize.py:157-205` — replace inline meta construction with helper call.

- [ ] **Step 1: Add the helper immediately above `fetch_article` (after line 156)**

Insert this function in `summarize.py` directly before the `def fetch_article(url: str)` line:

```python
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
```

- [ ] **Step 2: Replace the inline meta construction in `fetch_article`**

In `summarize.py`, replace lines 191-205 (the block starting with `md = extract_metadata(downloaded)` and ending with `return meta, text`) with:

```python
    return _meta_from_trafilatura(extract_metadata(downloaded), url, text), text
```

The function should end at `return _meta_from_trafilatura(...)` — drop the intermediate `title`/`author`/`site`/`published`/`meta = {...}` lines entirely.

- [ ] **Step 3: Smoke-verify the refactor**

Run against a known-working article (any plain blog post or news article that previously worked):

```bash
summarize --article https://simonwillison.net/2024/Dec/31/llms-in-2024/
```

Expected: Same behavior as before — fetches, summarizes, writes a `.md` file to `./summaries/`. Title and metadata should appear correctly in the output header. Compare the new output to any previously-generated summary of the same URL; the meta-dict fields should match.

- [ ] **Step 4: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
extract _meta_from_trafilatura helper

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `playwright` to packaging

**Why:** Task 3 imports `playwright.sync_api`. Without packaging in place, neither install path works.

**Files:**
- Modify: `pyproject.toml:19-20` — add `playwright` extra.
- Modify: `requirements.txt` — append `playwright`.

- [ ] **Step 1: Add the playwright extra to `pyproject.toml`**

Change the `[project.optional-dependencies]` block from:

```toml
[project.optional-dependencies]
transcribe = ["pywhispercpp"]
```

to:

```toml
[project.optional-dependencies]
transcribe = ["pywhispercpp"]
playwright = ["playwright"]
```

- [ ] **Step 2: Append `playwright` to `requirements.txt`**

Add a single new line after `pywhispercpp`:

```
anthropic
youtube-transcript-api
yt-dlp
trafilatura
pywhispercpp
playwright
```

- [ ] **Step 3: Install the extra and the browser binary**

```bash
uv tool install --force '.[playwright]'
playwright install chromium
```

Expected: `uv tool install` succeeds; `playwright install chromium` downloads Chromium (~150MB) into `~/Library/Caches/ms-playwright/` on macOS. Both commands exit 0.

- [ ] **Step 4: Verify Playwright is importable from the installed CLI**

```bash
summarize --help
```

Expected: Help text prints normally (no import errors at startup — Playwright should still be lazy-imported in later tasks). Then verify the package is actually installed:

```bash
python -c "import playwright.sync_api; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements.txt
git commit -m "$(cat <<'EOF'
add playwright optional extra for JS-rendered article fetch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add `fetch_article_playwright()` function

**Files:**
- Modify: `summarize.py` — add new function after `fetch_article` (somewhere around line 206 in the file's current state, immediately after the existing `fetch_article` body).

- [ ] **Step 1: Add the new function**

Insert this function in `summarize.py` immediately after the existing `fetch_article` function (after its `return _meta_from_trafilatura(...)` line):

```python
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
            page.goto(url, wait_until="networkidle")
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
```

- [ ] **Step 2: Smoke-verify directly via Python**

The function isn't wired into the CLI yet, so invoke it from a Python REPL or one-liner:

```bash
python -c "
from summarize import fetch_article_playwright
meta, text = fetch_article_playwright('https://www.morningstar.com/funds/theres-big-retirement-problem-vanguard-takes-step-toward-solving-it')
print('TITLE:', meta['title'])
print('WORDS:', meta['word_count'])
print('FIRST 200 CHARS:', text[:200])
"
```

Expected:
- TITLE includes "Retirement Problem" and "Vanguard"
- WORDS is >500 (the Q&A is ~900 words)
- FIRST 200 CHARS contains visible article text, not navigation chrome

If the title is "Untitled" or word count is suspiciously low (<100), trafilatura's metadata extraction isn't picking up the rendered DOM properly — investigate before continuing.

- [ ] **Step 3: Verify the missing-package error message**

Temporarily simulate the missing-package case by renaming the installed `playwright` directory or by uninstalling the extra in a scratch venv. Easiest: run the import in a python that doesn't have it:

```bash
# In a fresh shell with no playwright installed:
python3 -c "
import sys
sys.modules['playwright'] = None  # simulate missing
from summarize import fetch_article_playwright
try:
    fetch_article_playwright('https://example.com')
except RuntimeError as e:
    print('OK:', e)
"
```

Expected: prints `OK: playwright not installed. Run: uv tool install '.[playwright]' && playwright install chromium`

(Skip this step if simulating the missing import is awkward — the error path is small and easy to read; manual code review is acceptable.)

- [ ] **Step 4: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
add fetch_article_playwright for JS-rendered sites

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Refactor `fetch_article()` into a dispatcher

**Files:**
- Modify: `summarize.py` — rename the existing trafilatura body to `_fetch_article_trafilatura()` and replace `fetch_article` with a thin dispatcher that adds `force_playwright` and auto-fallback.

- [ ] **Step 1: Rename the existing function body**

Rename `def fetch_article(url: str) -> tuple[dict, str]:` to `def _fetch_article_trafilatura(url: str) -> tuple[dict, str]:`. Keep the body unchanged. Update the docstring's first line to:

```python
def _fetch_article_trafilatura(url: str) -> tuple[dict, str]:
    """Download a web article via trafilatura and return (meta, markdown_text).

    Uses trafilatura for boilerplate-free body extraction. Output is
    Markdown so chunk_article can split on '##'/'###' headings directly.
    """
```

- [ ] **Step 2: Add the new dispatcher `fetch_article`**

Insert this function immediately above `_fetch_article_trafilatura` (taking the slot the old `fetch_article` used to occupy):

```python
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
```

- [ ] **Step 3: Verify the happy path still works (trafilatura succeeds, no Playwright touched)**

```bash
summarize --article https://simonwillison.net/2024/Dec/31/llms-in-2024/
```

Expected: Identical behavior to Task 1's verification. trafilatura handles it; Playwright never launches (no Chromium process visible in Activity Monitor / `ps aux | grep chromium`).

- [ ] **Step 4: Verify the auto-fallback path works**

```bash
summarize --article 'https://www.morningstar.com/funds/theres-big-retirement-problem-vanguard-takes-step-toward-solving-it'
```

Expected: trafilatura fails (likely with `failed to fetch ... HTTP 202` or `no article content extracted`), then Playwright takes over transparently, then a summary is produced. Stderr output shows the fetch step succeeding eventually.

- [ ] **Step 5: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
auto-fall back to Playwright when trafilatura fails

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Add `--playwright` CLI flag

**Files:**
- Modify: `summarize.py:702-722` — thread `force_playwright` through `process_article`.
- Modify: `summarize.py:730-741` — add `--playwright` argparse flag.
- Modify: `summarize.py:765` — pass `args.playwright` to `process_article`.
- Modify: `summarize.py:607,623` — add `--playwright` to bash completion.

- [ ] **Step 1: Update `process_article` signature**

Change the signature at `summarize.py:702-704` from:

```python
def process_article(
    url: str, client: Anthropic, model: str, out_dir: Path
) -> Path | None:
```

to:

```python
def process_article(
    url: str, client: Anthropic, model: str, out_dir: Path,
    *, force_playwright: bool = False,
) -> Path | None:
```

And update the `fetch_article` call inside the function body (currently at line 708):

```python
        meta, text = fetch_article(url, force_playwright=force_playwright)
```

- [ ] **Step 2: Add the `--playwright` argparse flag**

In `main()` (after the existing `--completion` argument near line 740), add:

```python
    parser.add_argument(
        "--playwright",
        action="store_true",
        help="Force Playwright (Chromium) for article fetch (default: auto-fallback on failure; only meaningful with --article)",
    )
```

- [ ] **Step 3: Pass the flag through**

Change the `--article` branch in `main()` at line 765 from:

```python
    elif args.article:
        process_article(args.article, client, args.model, out_dir)
```

to:

```python
    elif args.article:
        process_article(args.article, client, args.model, out_dir, force_playwright=args.playwright)
```

- [ ] **Step 4: Update bash completion**

In the `BASH_COMPLETION` heredoc at `summarize.py:607`, change:

```
    opts="--batch --transcript-file --article --title --source --out-dir --model --completion --help"
```

to:

```
    opts="--batch --transcript-file --article --playwright --title --source --out-dir --model --completion --help"
```

No change to the `case "$prev"` block is needed — `--playwright` is a boolean flag that doesn't consume the next argument.

- [ ] **Step 5: Verify forced mode**

```bash
summarize --article https://simonwillison.net/2024/Dec/31/llms-in-2024/ --playwright
```

Expected: Even though trafilatura would succeed, Chromium launches (you can confirm with `ps aux | grep -i chrom` during the run), the article gets fetched via Playwright, summary is produced. Output should be qualitatively similar to the trafilatura version.

- [ ] **Step 6: Verify the bash completion script includes the new flag**

```bash
summarize --completion | grep -o '\-\-playwright'
```

Expected: prints `--playwright` (one match).

- [ ] **Step 7: Commit**

```bash
git add summarize.py
git commit -m "$(cat <<'EOF'
add --playwright flag to force Chromium-based article fetch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Update README and CLAUDE.md

**Files:**
- Modify: `README.md:17-21` — add JS-rendered article install section after the `[transcribe]` block.
- Modify: `README.md:76-83` — add `--playwright` to the usage block.
- Modify: `CLAUDE.md` — remove the Playwright future-work bullet, update the X.com gotcha, add an architecture note.

- [ ] **Step 1: Add the README install section**

After the existing `[transcribe]` block at `README.md:17-23` (after the line `Update later with \`uv tool upgrade summary-tools\`...`), insert:

````markdown
To fetch JS-rendered articles (X.com long-form, Morningstar Q&A, similar) via Playwright:

```bash
uv tool install '.[playwright]'
playwright install chromium       # one-time, ~150 MB Chromium download
```

Then either run `summarize --article <url> --playwright` to force, or let auto-fallback trigger when the default fetcher fails.
````

- [ ] **Step 2: Add `--playwright` to the README usage block**

In `README.md:76-83`, after the `summarize --article <url>` line, add:

```
summarize --article <url> --playwright   # force Chromium (for JS-rendered sites)
```

- [ ] **Step 3: Remove the Playwright future-work bullet from CLAUDE.md**

Delete the entire bullet starting `- **Playwright fallback for JS-rendered articles.**` and ending `...how to derive title/author/published metadata when \`trafilatura\`'s extractor isn't in the loop.` (the bullet most recently added in commit `e6330cc`).

- [ ] **Step 4: Update the X.com gotcha in CLAUDE.md**

Replace the existing X.com gotcha at `CLAUDE.md:66`:

```
- **X.com / Twitter articles** fall outside `--article` mode. `trafilatura` can't render the JS-only SPA, plain HTTP fetchers get a 402 from X's gateway, and yt-dlp errors with `Unsupported URL` on `/article/` long-form posts (it handles regular tweets fine). Playwright is the only working path so far.
```

with:

```
- **X.com / Twitter articles and other JS-rendered sites.** `trafilatura` can't render JS-only SPAs (X.com, Morningstar Q&A pages), and plain HTTP fetchers get 402/202 responses from various bot-protection gateways. Use `--playwright` (or let auto-fallback handle it) when the default path fails. Requires the `[playwright]` extra and `playwright install chromium`.
```

- [ ] **Step 5: Add an architecture note in CLAUDE.md**

In CLAUDE.md, find the article-mode description (currently around line 38-43, the "**Article mode** (`--article <url>`)..." block describing stages 1-4). At the end of that block (after the line describing stage 5 reuse), add this paragraph:

```
`fetch_article` is a dispatcher: by default it tries trafilatura (cheap HTTP), then auto-falls back to `fetch_article_playwright()` on any `RuntimeError`. Passing `--playwright` (CLI) or `force_playwright=True` (function call) skips the trafilatura attempt. The Playwright path renders the page in headless Chromium, hands `page.content()` to the same `trafilatura.extract()` used on the cheap path, so meta-dict shape and Markdown structure are identical across both fetchers — `chunk_article` and downstream stages never know which path ran.
```

- [ ] **Step 6: Read both files to confirm the edits**

```bash
grep -n "playwright\|Playwright" /Volumes/data2/scottb/dev/bartram/summary-tools/README.md /Volumes/data2/scottb/dev/bartram/summary-tools/CLAUDE.md
```

Expected: matches in README.md (install section + usage block) and CLAUDE.md (gotcha + architecture note). The old "Possible future work" bullet should NOT appear.

- [ ] **Step 7: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "$(cat <<'EOF'
document --playwright flag and JS-rendered article install

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: End-to-end verification

A final pass through the test plan from the spec to catch anything the per-task verifications missed.

- [ ] **Step 1: Trafilatura happy path unaffected**

```bash
summarize --article https://simonwillison.net/2024/Dec/31/llms-in-2024/
```

Expected: succeeds without launching Chromium. Output is a `.md` file in `./summaries/`.

- [ ] **Step 2: Auto-fallback on Morningstar**

```bash
summarize --article 'https://www.morningstar.com/funds/theres-big-retirement-problem-vanguard-takes-step-toward-solving-it'
```

Expected: trafilatura fails (HTTP 202 or empty extraction), Playwright takes over silently, summary is produced.

- [ ] **Step 3: Forced Playwright**

```bash
summarize --article https://simonwillison.net/2024/Dec/31/llms-in-2024/ --playwright
```

Expected: Chromium launches even though trafilatura would have worked. Summary is produced.

- [ ] **Step 4: Missing-extra path (manual code review acceptable)**

In a temporary venv without the `[playwright]` extra:

```bash
python -m venv /tmp/no-pw-venv
source /tmp/no-pw-venv/bin/activate
pip install anthropic youtube-transcript-api yt-dlp trafilatura
pip install -e /Volumes/data2/scottb/dev/bartram/summary-tools
summarize --article 'https://www.morningstar.com/funds/theres-big-retirement-problem-vanguard-takes-step-toward-solving-it'
deactivate
```

Expected: trafilatura's original error message is shown, suffixed with ` (install '[playwright]' extra to enable fallback for JS-rendered sites)`. Then with `--playwright`:

```bash
source /tmp/no-pw-venv/bin/activate
summarize --article https://example.com --playwright
deactivate
```

Expected: `playwright not installed. Run: uv tool install '.[playwright]' && playwright install chromium`.

(If creating a scratch venv feels heavy, code-review the error paths in `summarize.py` — both branches are ~5 lines each.)

- [ ] **Step 5: Final commit hygiene check**

```bash
git log --oneline -8
```

Expected: Six new commits (Tasks 1–6) on top of `4ca3461`. Each commit message is lowercase imperative, no trailing period.

```bash
git status
```

Expected: clean working tree.

---

## Summary

After completion, the feature shape is:

- Default behavior unchanged: `summarize --article <url>` still uses trafilatura first.
- New: trafilatura failure auto-falls back to Playwright if the `[playwright]` extra is installed.
- New: `--playwright` flag forces Chromium from the start.
- New: clear install hints in error messages when the extra is missing.
- Spec's "Possible future work" bullet is now in code; CLAUDE.md and README updated accordingly.
