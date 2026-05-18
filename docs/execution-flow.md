# Execution flow: `summarize.py`

Top-down view of how a single invocation flows through the script,
including the libraries it uses and the external systems it talks to.

```mermaid
flowchart TD
    CLI["main()<br/>argparse"]:::entry

    CLI -->|"&lt;url&gt;"| PU[process_url]
    CLI -->|"--batch FILE"| BATCH["loop URLs<br/>→ process_url"]
    CLI -->|"--transcript-file FILE"| PT[process_transcript_file]
    CLI -->|"--article URL"| PA[process_article]
    CLI -->|"--completion"| COMP["print BASH_COMPLETION<br/>and exit"]:::leaf

    BATCH --> PU

    %% --- YouTube pipeline ---
    subgraph YT["YouTube pipeline"]
        direction TB
        PU --> EVID[extract_video_id]
        EVID --> FMETA["fetch_metadata<br/>(yt-dlp)"]
        FMETA --> FTRANS["fetch_transcript<br/>(youtube-transcript-api)"]
        FTRANS --> CT["chunk_transcript<br/>15-min windows<br/>1-min overlap<br/>inject [timestamp] markers"]
        CT --> SUM[summarize]
    end

    %% --- Local transcript pipeline ---
    subgraph LOCAL["Local transcript pipeline"]
        direction TB
        PT --> PARSE["parse_transcript_file<br/>whisper-style lines"]
        PARSE --> CT2["chunk_transcript<br/>(reused)"]
        CT2 --> SUM
    end

    %% --- Article pipeline ---
    subgraph ART["Article pipeline"]
        direction TB
        PA --> FA["fetch_article<br/>(dispatcher)"]
        FA -->|"default"| TRAFI["_fetch_article_trafilatura<br/>(HTTP + boilerplate strip)"]
        FA -->|"--playwright<br/>or RuntimeError fallback"| PW["fetch_article_playwright<br/>(headless Chromium)<br/>→ trafilatura.extract"]
        TRAFI --> CA["chunk_article<br/>split on H2,<br/>subdivide on H3 if &gt;2500 words"]
        PW --> CA
        CA --> SUMA[summarize_article]
    end

    %% --- Shared LLM + output ---
    SUM --> CC["call_claude<br/>1 call (single-pass)<br/>or N+1 calls (chunk + synthesis)"]
    SUMA --> CC
    CC --> WS["write_summary<br/>slugify + YYYY-MM-DD prefix"]
    WS --> OUT[("./summaries/*.md")]:::leaf

    %% --- External systems ---
    FMETA -. HTTPS .-> YTSITE[(YouTube)]
    FTRANS -. HTTPS .-> YTAPI[(YouTube timed-text)]
    TRAFI -. HTTPS .-> WEB[(arbitrary origin)]
    PW -. HTTPS via Chromium .-> WEB
    CC -. HTTPS .-> ANTHROPIC[(Anthropic Messages API)]
    PARSE -. read .-> FS[(local filesystem)]
    WS -. write .-> FS

    classDef entry fill:#1e3a5f,stroke:#5a9fd4,color:#fff
    classDef leaf fill:#2d5a2d,stroke:#7ac77a,color:#fff
```

## Libraries & external systems

| Stage | Library | External system |
| --- | --- | --- |
| Video metadata | `yt-dlp` | YouTube (HTTPS) |
| Video transcript | `youtube-transcript-api` | YouTube timed-text API |
| Article fetch (cheap path) | `trafilatura` (`fetch_url` + `extract`) | Arbitrary HTTP origin |
| Article fetch (fallback) | `playwright` (sync, headless Chromium) + `trafilatura.extract` | Same origin, JS-rendered |
| Local transcript | stdlib `re` + `pathlib` | Local filesystem |
| LLM calls | `anthropic.Anthropic` | Anthropic Messages API |
| CLI | stdlib `argparse` | — |
| Output | stdlib `pathlib` | Local filesystem (`./summaries/`) |

## Key shape

- **Four CLI dispatch branches** in `main()` route to one of three pipelines
  (plus the trivial `--completion` exit).
- **Each pipeline converges on a shared tail**: `summarize*` →
  `call_claude` → `write_summary`. The output format and slugified filename
  are identical regardless of source.
- **`call_claude` is the single chokepoint** to the Anthropic API. The
  `Anthropic` client is constructed once in `main()` with
  `max_retries=API_MAX_RETRIES`, so retry behavior and connection reuse are
  consistent across every LLM call (chunk, synthesis, single-pass, article
  variants).
- **`fetch_article` is try-cheap-then-fall-back**: trafilatura's plain HTTP
  first, Playwright/Chromium only if a `RuntimeError` bubbles. Both paths
  hand HTML to the same `trafilatura.extract()` call, so the meta dict and
  Markdown shape are identical downstream — variance at the edges,
  uniformity in the middle.
