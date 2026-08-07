# Possible future work

Moved from CLAUDE.md (kept out of always-loaded context; see the pointer there).

- **Whisper fallback** for caption-less videos. `yt-dlp -x --audio-format mp3` → `whisper.cpp` locally or OpenAI transcription API. Same `[{text, start, duration}]` shape so downstream code doesn't change.
- **Content-hash caching** to skip already-summarized videos in batch mode. Hash the video ID + transcript, store summary path in a sidecar JSON.
- **Configurable output template.** Hardcoded prompts now; could move to a `prompts/` directory for easy customization (study notes vs. terse bullets vs. current detailed format).
- **Channel/playlist mode.** `yt-dlp` can enumerate playlists; would slot into the existing batch path.
- **Stdin → knowledge-base pipe.** New script (or `--kb` flag on `summarize`) that reads URLs from stdin, one per line, auto-detects YouTube vs. article, runs the appropriate summarize pipeline, then copies the resulting `.md` into a `~/Dropbox/kb/<raw-dir>/` knowledge-base location. Unblocks `cat urls.txt | … ` and ad-hoc piping from clipboard/`pbpaste`. Open questions: how to choose which raw dir (CLI flag vs. per-URL prefix vs. content-type-based default), whether to move or copy, and whether existing summaries in `./summaries/` should still be kept as the source of truth.
