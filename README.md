# summary-tools

Python CLI that summarizes YouTube videos, transcripts, and web articles with Claude.
Pulls captions (or extracts article body), chunks the text, and writes a Markdown file with TL;DR, key takeaways, walkthrough, and notable quotes.

See [CLAUDE.md](./CLAUDE.md) for architecture, design decisions, and known gotchas.

## Install globally (recommended)

If you just want the `summarize` CLI on your `$PATH` from any directory, use [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/):

```bash
uv tool install .                 # or: pipx install .
summarize <url>                   # now works from anywhere
```

To include the `run_transcribe.py` companion (pulls in `pywhispercpp`, a C extension):

```bash
uv tool install '.[transcribe]'
```

Update later with `uv tool upgrade summary-tools`; uninstall with `uv tool uninstall summary-tools`.

To fetch JS-rendered articles (X.com long-form, Morningstar Q&A, similar) via Playwright:

```bash
uv tool install '.[playwright]'
playwright install chromium       # one-time, ~150 MB Chromium download
```

Then either run `summarize --article <url> --playwright` to force, or let auto-fallback trigger when the default fetcher fails.

You still need `ANTHROPIC_API_KEY` exported in your shell (see step 5 below).

## Dev setup (editable venv)

Use this if you want to hack on the code itself rather than just run it.

### 1. Download the Whisper model

`run_transcribe.py` (the cloud-sandbox transcription companion) needs `ggml-tiny.en.bin` in the repo root. Download it from the official whisper.cpp Hugging Face repo:

```bash
curl -L -o ggml-tiny.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin
```

The file is ~78 MB and is gitignored.

### 2. Create a virtual environment

```bash
python -m venv .venv
```

### 3. Activate it

```bash
source .venv/bin/activate
```

On Git Bash for Windows: `source .venv/Scripts/activate`

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Set your Anthropic API key

```bash
cp .envrc-example .envrc
# edit .envrc and set your anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...
# export it to the local shell
direnv allow
```

## Usage

After `uv tool install` just use `summarize`. In a dev venv, use `python summarize.py`.

```bash
summarize <url>                          # single video
summarize --batch urls.txt               # one URL per line; '#' lines ignored
summarize <url> --out-dir ./notes        # custom output directory
summarize <url> --model claude-haiku-4-5-20251001
summarize --transcript-file FILE [--title "..."] [--source "..."]
summarize --article <url>                # web article (e.g. blog post)
summarize --article <url> --playwright   # force Chromium (for JS-rendered sites)
```

Output lands in `./summaries/YYYY-MM-DD_<slug>.md`.
