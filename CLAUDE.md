# YouTube Video & Web Article Summarizer

A Python CLI that pulls YouTube transcripts or web article bodies and summarizes them with Claude. Outputs a Markdown file with TL;DR, key takeaways, walkthrough (timestamped for video, section-headed for articles), and notable quotes.

## Setup

See [README.md](./README.md). Two install paths:

- **Global CLI**: `uv tool install .` (or `pipx install .`) → `summarize` on `$PATH` from any directory. Driven by `pyproject.toml`.
- **Dev venv**: `pip install -r requirements.txt` in a `.venv`, then `python summarize.py <url>`.

`pywhispercpp` is for `run_transcribe.py` only. It's an optional extra in `pyproject.toml` (`uv tool install '.[transcribe]'`) and still listed in `requirements.txt` for the venv flow.

## Usage

After install, invoke `summarize` from anywhere. During dev, `python summarize.py …` works the same.

```bash
summarize <url>                          # single video
summarize --batch urls.txt               # one URL per line; '#' lines ignored
summarize <url> --out-dir ./notes        # custom output directory
summarize <url> --model claude-haiku-4-5-20251001
summarize --transcript-file FILE [--title "..."] [--source "..."]
summarize --article <url>                # web article (e.g. blog post)
summarize --completion                   # emit bash completion script
```

Output: `./summaries/YYYY-MM-DD_<slug>.md`

## Architecture

Five replaceable stages, all in `yt_summarize.py`:

1. **`fetch_metadata(url)`** — `yt-dlp` for title/channel/duration without downloading.
2. **`fetch_transcript(video_id)`** — `youtube-transcript-api`, returns `[{text, start, duration}]` or `None`.
3. **`chunk_transcript(segments)`** — 15-min windows with 1-min overlap. Single chunk if video ≤ 15 min.
4. **`summarize(chunks, meta, ...)`** — one Claude call for short videos; per-chunk summaries + synthesis pass for long ones. `[timestamp]` markers injected every ~30s so the model can cite them.
5. **`write_summary(summary, meta, out_dir)`** — Markdown file, slugified filename.

**Local transcript mode** (`--transcript-file`) replaces stages 1–2 with `parse_transcript_file()`, which reads whisper-style lines `[MM:SS --> MM:SS]  text` (also supports `H:MM:SS`) into the same `{text, start, duration}` shape. Title is inferred from filename (strips trailing `_transcript`); duration is computed from the last segment. Stages 3–5 run unchanged.

**Article mode** (`--article <url>`) is a parallel pipeline:
1. `fetch_article(url)` — `trafilatura` does fetch + boilerplate-stripped body extraction. Returns `(meta, markdown_text)` where meta has title/author/site/published/url/word_count.
2. `chunk_article(text)` — splits on Markdown `##`/`###` headings (preferring H2). Articles under ~6k words or with no usable headings stay as a single chunk.
3. `summarize_article(chunks, meta, ...)` — uses dedicated `ARTICLE_*` prompts (no timestamp machinery). Single-pass for one chunk, per-section summaries + synthesis for many.
4. Stage 5 (`write_summary`) is reused as-is.

## Design decisions

- **Captions only.** No Whisper fallback. Most YouTube content has auto-captions, and skipping Whisper keeps deps light. Add it later if a real need shows up.
- **Sonnet throughout** (`claude-sonnet-4-6`). Good cost/quality balance. Haiku for cheap testing, Opus only if quality complaints arise.
- **15-min chunks, 1-min overlap.** Long enough for coherent sections, short enough to keep summaries detailed. Overlap preserves continuity across boundaries.
- **Timestamps preserved end-to-end.** Injected into the chunk text, kept by the model in chunk summaries, kept again in the final synthesis. Format: `[M:SS]` or `[H:MM:SS]`.
- **Markdown output structure is fixed** in the prompts (TL;DR / Key Takeaways / Walkthrough / Notable Quotes). Quotes section omitted if nothing strong.
- **Model agnostic CLI** via `--model` so swapping is trivial.

## Cost estimate

Roughly $0.05–0.15 per hour of video on Sonnet. A 1-hour video typically runs as 4 chunk calls + 1 synthesis call = 5 API calls.

## Known gotchas

- **`youtube-transcript-api` v0 vs v1.** The fetch function tries `YouTubeTranscriptApi().fetch()` (v1.x) and falls back to `YouTubeTranscriptApi.get_transcript()` (v0.x). If you hit weird errors, `pip install -U youtube-transcript-api`.
- **Cloud IP blocking.** YouTube blocks transcript requests from many VPS providers. Works fine from a home connection. From a cloud host you'd need to configure cookies via `yt-dlp`.
- **No transcript ≠ no video.** Live streams, age-restricted content, and some music videos return `None` from `fetch_transcript`. The script logs and skips.
- **X.com / Twitter articles** fall outside `--article` mode. `trafilatura` can't render the JS-only SPA, plain HTTP fetchers get a 402 from X's gateway, and yt-dlp errors with `Unsupported URL` on `/article/` long-form posts (it handles regular tweets fine). Playwright is the only working path so far.

## Possible future work

- **Whisper fallback** for caption-less videos. `yt-dlp -x --audio-format mp3` → `whisper.cpp` locally or OpenAI transcription API. Same `[{text, start, duration}]` shape so downstream code doesn't change.
- **Content-hash caching** to skip already-summarized videos in batch mode. Hash the video ID + transcript, store summary path in a sidecar JSON.
- **Configurable output template.** Hardcoded prompts now; could move to a `prompts/` directory for easy customization (study notes vs. terse bullets vs. current detailed format).
- **Channel/playlist mode.** `yt-dlp` can enumerate playlists; would slot into the existing batch path.
- **Stdin → knowledge-base pipe.** New script (or `--kb` flag on `summarize`) that reads URLs from stdin, one per line, auto-detects YouTube vs. article, runs the appropriate summarize pipeline, then copies the resulting `.md` into a `~/Dropbox/kb/<raw-dir>/` knowledge-base location. Unblocks `cat urls.txt | … ` and ad-hoc piping from clipboard/`pbpaste`. Open questions: how to choose which raw dir (CLI flag vs. per-URL prefix vs. content-type-based default), whether to move or copy, and whether existing summaries in `./summaries/` should still be kept as the source of truth.

## Companion: `run_transcribe.py`

Runs in a Claude Code cloud sandbox (paths under `/sessions/.../mnt/summary-tools/`). Transcribes `/tmp/podcast.wav` with `pywhispercpp` + `ggml-tiny.en.bin` and writes `<name>_transcript.txt` in the whisper format consumed by `--transcript-file`. The hardcoded sandbox paths are intentional — don't "fix" them for local use.

The `ggml-tiny.en.bin` weights come from `https://huggingface.co/ggerganov/whisper.cpp` (gitignored, ~78 MB). README.md has the curl command.

