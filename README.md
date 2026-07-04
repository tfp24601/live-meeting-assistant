# Live Meeting Assistant

A self-hosted live meeting copilot. It listens to your meeting, transcribes it
in real time on your own GPU, and streams **talking-point suggestion cards**
into a side panel — grounded in *your own* published writing, docs, and
transcripts (RAG), with on-demand web research.

Inspired by [Wirasm/dylan-record](https://github.com/Wirasm/dylan-record)
(a macOS/Swift meeting recorder). This is its cross-platform sibling: capture
happens in a **browser page** (Windows/Linux/macOS/ARM — anything that runs
Chrome or Edge), and the heavy lifting runs on a **Linux GPU box** you own.
Your meeting audio never has to leave your network.

## What it does

- **Live transcript** — your mic + the meeting's system audio, streamed as
  16 kHz PCM over WebSockets to local
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on your GPU.
  Speaker-echo suppression keeps "Them" from reappearing as "You" when you
  use laptop speakers.
- **Suggestion cards** — every ~12s of fresh conversation, an LLM produces
  2–4 cards: **SAY** (a point to make), **ASK** (a sharp question), **FACT**,
  **IDEA**. Pin the good ones; export the transcript as VTT or Markdown.
- **Your knowledge, cited** — ingest your blog (Ghost sitemaps), markdown
  docs, and diarized transcripts into qdrant; relevant passages are retrieved
  every run and cards cite their source.
- **🌐 Deep dive** — an on-demand slower run that may search the web and cite
  domains (provider-dependent).
- **In-person mode** — one mic hears the room; each utterance is
  voice-matched against your enrolled voice (local ECAPA embeddings), so the
  transcript still splits **You** vs **Others**. Enrollment is a 20-second
  in-page recording.
- **Settings screen** — add/remove/ignore knowledge sources, switch LLM
  provider, tune cadence and thresholds. Most changes apply live.

## LLM providers

| Provider | Needs | Notes |
| --- | --- | --- |
| `claude-cli` (default) | `claude` CLI logged in on the host | Bills to a Claude Pro/Max subscription; enables web-search deep dives |
| `anthropic-api` | `ANTHROPIC_API_KEY` | Pay-as-you-go |
| `openai-compatible` | base URL + key | Ollama, OpenRouter, LM Studio, vLLM, … |
| `custom-command` | any shell command | Prompt on stdin, reply on stdout — wire in anything |

## Quickstart

Requirements: Linux host, Python 3.11+, `ffmpeg`; NVIDIA GPU recommended for
whisper (CPU works with small models); a [qdrant](https://qdrant.tech)
instance (`docker run -p 6333:6333 qdrant/qdrant`).

```bash
git clone <this repo> && cd LiveMeetingAssistant
./deploy/setup-venv.sh                          # venv + deps (+ CUDA libs)
.venv/bin/pip install -r backend/requirements-ingest.txt   # if ingesting here
cp backend/app/.env.example lma.env             # edit: name, provider, qdrant
cp sources.example.yaml sources.yaml            # edit: your knowledge sources
./backend/run.sh                                # http://localhost:5005
.venv/bin/python -m backend.ingest.run_all      # build the knowledge base
```

Run it as a service: see `deploy/lma.service.example`. Deploy to a separate
GPU box: `./deploy/deploy-remote.sh user@host /path/`.

### HTTPS is required for capture

Browsers only expose mic/screen capture on HTTPS or localhost. The pleasant
self-hosted path is [Tailscale Serve](https://tailscale.com/kb/1312/serve):

```bash
tailscale serve --bg --https=8443 5005
# -> https://<host>.<tailnet>.ts.net:8443/  (trusted cert, tailnet-only)
```

Any reverse proxy with a valid cert works too.

### Using it

Open the page, pick **Online** or **In person** and **One-on-one** or
**Multiple**, hit **Start**. For online meetings, share the meeting
tab/window **with "Share audio" checked** so the other side is captured.
For in-person, enroll your voice once (🎙 button). Wear headphones online if
you can; the echo suppressor covers you if you can't.

## Security model

No authentication is built in — run it on a private network (tailnet, LAN,
VPN). Do not expose it to the public internet as-is: it accepts live audio
and can read your knowledge base. Meeting audio, transcripts, and voice
enrollment stay on your machines; the only outbound calls are to your chosen
LLM provider (and the web, during deep dives).

## Architecture

```text
  laptop in the meeting (browser)                GPU host (FastAPI)
  ┌──────────────────────────────┐   WS PCM     ┌─────────────────────────────┐
  │  mic  (getUserMedia)         │ ───────────► │ faster-whisper streaming    │
  │  tab  (getDisplayMedia)      │              │ echo suppression / speaker  │
  │  transcript + cards + pins   │ ◄─────────── │ verification                │
  └──────────────────────────────┘   WS JSON    │ qdrant RAG → LLM provider   │
                                                └─────────────────────────────┘
```

One collection (`lma_knowledge`, bge-base-en-v1.5) holds all sources with a
`source` payload key; disabling a source is a query-time filter, so its data
survives for instant re-enable. Ingestion is idempotent (deterministic point
ids, per-source stale pruning) — cron it nightly.

## License

MIT — see [LICENSE](LICENSE). Credit to
[dylan-record](https://github.com/Wirasm/dylan-record) for the original
spark: an agent that listens to your meetings and helps in real time.
