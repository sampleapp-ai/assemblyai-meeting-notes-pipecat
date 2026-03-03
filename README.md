# Meeting Notes — Pipecat + AssemblyAI Universal-3 Pro

A listen-only meeting transcription agent using Pipecat's pipeline framework and AssemblyAI's Universal-3 Pro streaming speech-to-text. The agent captures all speech in real time, displays a live transcript in the browser, and generates structured meeting notes at session end via an LLM call. No TTS output — this is a passive listener with a trimmed pipeline.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Browser (Custom Frontend)                      │
│  ┌──────────────┐  ┌────────────────────────┐   │
│  │  Microphone   │  │  Live Transcript       │   │
│  │  (Daily.co)   │  │  + Meeting Notes       │   │
│  └──────┬───────┘  │  (WebSocket)            │   │
│         │           └────────────┬───────────┘   │
└─────────┼────────────────────────┼───────────────┘
          │ audio via Daily        │ transcripts/notes
          ▼                        ▼
┌─────────────────────────────────────────────────┐
│  Python Backend (FastAPI + Pipecat)             │
│                                                 │
│  Daily Room → AssemblyAI STT → Collector        │
│                                     │           │
│                        ┌────────────┘           │
│                        ▼                        │
│              Broadcast via WebSocket            │
│                        │                        │
│              [on disconnect]                    │
│                        ▼                        │
│              Cerebras LLM → Meeting Notes       │
│                        │                        │
│              Broadcast via WebSocket            │
└─────────────────────────────────────────────────┘
```

- **STT**: AssemblyAI Universal-3 Pro — sub-300ms streaming transcription with punctuation-based turn detection
- **LLM**: Cerebras (`llama3.1-8b`) — generates structured meeting notes from the collected transcript at session end
- **Transport**: Daily.co for audio (handles WebRTC infrastructure including TURN), WebSocket for transcript/notes streaming
- **Frontend**: Custom HTML/JS/CSS served by FastAPI with Daily JS SDK
- **No TTS**: This is a listen-only agent — no audio output

## Prerequisites

- Python 3.10+
- API keys for AssemblyAI, Cerebras, and Daily.co

## Setup

1. Clone the repo and navigate to this directory:

```bash
cd meeting-notes/pipecat
```

2. Create a virtual environment and install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

You'll need:
- **AssemblyAI**: [assemblyai.com/dashboard/api-keys](https://www.assemblyai.com/dashboard/api-keys)
- **Cerebras**: [cloud.cerebras.ai](https://cloud.cerebras.ai)
- **Daily.co**: [dashboard.daily.co](https://dashboard.daily.co) (free tier: 10,000 min/mo)

## Running Locally

```bash
python meeting_notes.py
```

Open `http://localhost:7860` in your browser. Click **Start Meeting** — the agent transcribes speech in real time and displays it in the live transcript panel. Click **End Meeting** to generate structured meeting notes.

> **Note:** Local development also uses Daily transport by default. To use SmallWebRTC locally instead (no Daily API key needed), run: `python meeting_notes.py --transport webrtc`

## Key Features

- **Custom frontend**: Browser-based UI with live transcript display and formatted meeting notes.
- **Real-time transcript streaming**: Each finalized transcript turn is broadcast to the browser via WebSocket as it arrives, with interim/partial results shown in real time.
- **Transcript segment merging**: Consecutive transcript fragments from the same speaker within a 3-second window are grouped into a single readable row.
- **Listen-only pipeline**: Trimmed Pipecat pipeline with no TTS or audio output — just `transport.input() → STT → TranscriptCollector`.
- **Keyterms boosting**: Meeting-specific terms are boosted for higher transcription accuracy.
- **Structured note generation**: On disconnect, the full transcript is sent to Cerebras LLM which produces notes with Summary, Key Discussion Points, Decisions Made, Action Items, and Next Steps.
- **Retry with backoff**: LLM calls retry up to 4 times with exponential backoff on rate limit errors.

## Configuration

### Keyterms

Update the `keyterms_prompt` array in `meeting_notes.py` to boost recognition of your meeting-specific terminology:

```python
keyterms_prompt=[
    "Alice Johnson", "Bob Smith", "Project Phoenix",
    "Q3 roadmap", "quarterly review", "action items",
    "deadline", "budget",
]
```

### VAD Parameters

Voice activity detection is configured in `meeting_notes.py`:

| Parameter | Value | Description |
|---|---|---|
| `confidence` | 0.6 | VAD confidence threshold |
| `start_secs` | 0.1 | Seconds of speech to trigger start |
| `stop_secs` | 0.8 | Silence before finalizing a segment |
| `min_volume` | 0.4 | Minimum volume threshold |

## Project Structure

```
meeting-notes/pipecat/
├── meeting_notes.py     # Pipecat agent: listen-only pipeline + note generation
├── run.py               # FastAPI server: Daily room management, WebSocket, static files
├── client/
│   ├── index.html       # Meeting notes UI (loads Daily JS SDK)
│   ├── style.css        # Dark theme styling
│   └── app.js           # Daily call object + WebSocket client logic
├── Dockerfile           # Production container
├── requirements.txt
├── .env.example
└── README.md
```

## Speaker Diarization Note

Speaker diarization is not currently available in AssemblyAI's streaming API. For speaker-labeled meeting notes, use a hybrid approach: stream during the meeting for live transcription, then process the recording through the [async API](https://www.assemblyai.com/docs) for speaker-diarized, summarized notes.
