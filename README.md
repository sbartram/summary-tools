# summarize

Python CLI that summarizes YouTube videos and web articles with Claude. Pulls captions (or extracts article body), chunks the text, and writes a Markdown file with TL;DR, key takeaways, walkthrough, and notable quotes.

See [CLAUDE.md](./CLAUDE.md) for architecture, design decisions, and known gotchas.

## Setup

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

```bash
python summarize <url>                          # single video
python summarize --batch urls.txt               # one URL per line; '#' lines ignored
python summarize <url> --out-dir ./notes        # custom output directory
python summarize <url> --model claude-haiku-4-5-20251001
python summarize --transcript-file FILE [--title "..."] [--source "..."]
python summarize --article <url>                # web article (e.g. blog post)
```

Output lands in `./summaries/YYYY-MM-DD_<slug>.md`.
