# Meeting Notes — Pipecat + AssemblyAI Universal-3 Pro

A listen-only meeting transcription agent using Pipecat's pipeline framework and AssemblyAI's Universal-3 Pro streaming speech-to-text. The agent captures all speech in real time, displays a live transcript in the browser, and generates structured meeting notes at session end via an LLM call. No TTS output — this is a passive listener with a trimmed pipeline.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Browser (Custom Frontend)                      │
│  ┌──────────────┐  ┌────────────────────────┐   │
│  │  Microphone   │  │  Live Transcript       │   │
│  │  (WebRTC)     │  │  + Meeting Notes       │   │
│  └──────┬───────┘  │  (WebSocket)            │   │
│         │           └────────────┬───────────┘   │
└─────────┼────────────────────────┼───────────────┘
          │ audio                  │ transcripts/notes
          ▼                        ▼
┌─────────────────────────────────────────────────┐
│  Python Backend (FastAPI + Pipecat)             │
│                                                 │
│  WebRTC Input → AssemblyAI STT → Collector      │
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

- **STT**: AssemblyAI Universal-3 Pro (`u3-rt-pro`) — sub-300ms streaming transcription with punctuation-based turn detection
- **LLM**: Cerebras (`llama-3.3-70b`) — generates structured meeting notes from the collected transcript at session end
- **Transport**: WebRTC for audio input, WebSocket for transcript/notes streaming
- **Frontend**: Custom HTML/JS/CSS served by FastAPI
- **No TTS**: This is a listen-only agent — no audio output

## Prerequisites

- Python 3.10+
- API keys for AssemblyAI and Cerebras

## Setup

1. Clone the repo and navigate to this directory:

```bash
cd meeting-notes/pipecat
```

2. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
```

## Running

```bash
python meeting_notes.py
```

Open `http://localhost:7860` in your browser. Click **Start Meeting** — the agent transcribes speech in real time and displays it in the live transcript panel. Click **End Meeting** to generate structured meeting notes.

## Key Features

- **Custom frontend**: Browser-based UI with live transcript display and formatted meeting notes — no prebuilt Pipecat UI.
- **Real-time transcript streaming**: Each finalized transcript turn is broadcast to the browser via WebSocket as it arrives, with interim/partial results shown in real time.
- **Listen-only pipeline**: Trimmed Pipecat pipeline with no TTS or audio output — just `transport.input() → STT → TranscriptCollector`.
- **Balanced turn detection**: `min_end_of_turn_silence_when_confident` set to 560ms and `max_turn_silence` to 2000ms, allowing speakers time to think between sentences.
- **Keyterms boosting**: Meeting-specific terms (participant names, project names, domain terms) are boosted for higher transcription accuracy.
- **Structured note generation**: On disconnect, the full transcript is sent to Cerebras LLM which produces notes with Summary, Key Discussion Points, Decisions Made, Action Items, and Next Steps. Notes are rendered as formatted markdown in the browser.

## Configuration

### Turn Detection

Adjust turn detection timing in the `AssemblyAIConnectionParams`:

| Parameter | Value | Description |
|---|---|---|
| `min_end_of_turn_silence_when_confident` | 560ms | Silence before speculative end-of-turn check — higher than a voice agent (100ms) to allow natural meeting pauses |
| `max_turn_silence` | 2000ms | Maximum silence before forcing turn end — higher than a voice agent (1200ms) to accommodate thinking pauses |

### Keyterms

Update the `keyterms_prompt` array to boost recognition of your meeting-specific terminology:

```python
keyterms_prompt=[
    "Alice Johnson", "Bob Smith", "Project Phoenix",
    "Q3 roadmap", "quarterly review", "action items",
    "deadline", "budget",
]
```

You can also update keyterms mid-stream using `UpdateConfiguration` if new topics arise during the meeting.

## Project Structure

```
meeting-notes/pipecat/
├── meeting_notes.py     # Pipecat agent: listen-only pipeline + note generation
├── run.py               # FastAPI server: WebRTC signaling, WebSocket, static files
├── client/
│   ├── index.html       # Meeting notes UI
│   ├── style.css        # Dark theme styling
│   └── app.js           # WebRTC + WebSocket client logic
├── requirements.txt
├── .env.example
└── README.md
```

## Voice Agent vs Meeting Notes Pipeline

| | Voice Agent | Meeting Notes |
|---|---|---|
| Pipeline | Input → STT → LLM → TTS → Output | Input → STT → TranscriptCollector |
| Audio output | Yes (TTS) | No |
| Frontend | Prebuilt Pipecat WebRTC UI | Custom HTML/JS with live transcript |
| Data to client | Audio (WebRTC) | Transcript + notes (WebSocket) |
| LLM usage | Real-time conversational responses | Post-session note generation |
| Turn detection | Aggressive (100ms / 1200ms) | Balanced (560ms / 2000ms) |
| Interruptions | Enabled | Disabled |

## Speaker Diarization Note

Speaker diarization is not currently available in AssemblyAI's streaming API. For speaker-labeled meeting notes, use a hybrid approach: stream during the meeting for live transcription, then process the recording through the [async API](https://www.assemblyai.com/docs) for speaker-diarized, summarized notes.
