# Build: Meeting Notes Agent with Pipecat + AssemblyAI Universal-3 Pro

## Goal

Build a listen-only meeting transcription agent using Pipecat's pipeline framework and AssemblyAI's Universal-3 Pro streaming STT. The agent captures all speech in real time, collects transcript turns, and generates structured meeting notes at session end via an LLM call. **No TTS output** — this is a passive listener with a trimmed pipeline (no TTS, no audio output).

---

## AssemblyAI Universal-3 Pro (U3P) Streaming Context

U3P (`speech_model: "u3-rt-pro"`) is optimized for real-time audio utterances under 10 seconds with sub-300ms time-to-complete-transcript latency. Highest accuracy for entities, rare words, and domain-specific terminology.

### Connection

WebSocket endpoint: `wss://streaming.assemblyai.com/v3/ws`

```json
{
  "speech_model": "u3-rt-pro",
  "sample_rate": 16000
}
```

### Punctuation-Based Turn Detection

| Parameter | Default | Description |
|---|---|---|
| `min_end_of_turn_silence_when_confident` | 100ms | Silence before a speculative EOT check fires. Model checks for terminal punctuation (`.` `?` `!`). |
| `max_turn_silence` | 1200ms | Maximum silence before a turn is forced to end, regardless of punctuation. |

**How it works:**
1. Silence reaches `min_end_of_turn_silence_when_confident` → model checks for terminal punctuation
2. Terminal punctuation found → turn ends (`end_of_turn: true`)
3. No terminal punctuation → partial emitted (`end_of_turn: false`), turn continues
4. Silence reaches `max_turn_silence` → turn forced to end (`end_of_turn: true`)

**Important:** `end_of_turn` and `turn_is_formatted` always have the same value.

### Prompting

**`keyterms_prompt`** — Boost recognition of specific names, brands, or domain terms:
```json
{ "keyterms_prompt": ["Alice Johnson", "Project Phoenix", "Q3 roadmap"] }
```

**`prompt`** — Behavioral/formatting instructions. When omitted, default prompt provides 88% turn detection accuracy.

**`prompt` and `keyterms_prompt` are mutually exclusive.**

### Mid-Stream Configuration Updates

```json
{
  "type": "UpdateConfiguration",
  "keyterms_prompt": ["budget review", "Q4 targets"],
  "max_turn_silence": 3000
}
```

### ForceEndpoint

```json
{ "type": "ForceEndpoint" }
```

### Not Available in Streaming

- **Speaker diarization** — Coming Soon
- **PII redaction** — Async-only
- **Summarization, sentiment analysis, entity detection** — Async-only

> **Hybrid approach:** Stream during the meeting for live captions, then process the recording through the async API for speaker-diarized, summarized notes.

---

## Use Case: Meeting Notes — Live Meeting Summarizer

Meeting assistant that transcribes in real time and generates structured notes at the end.

**U3P features used:**

| Feature | How it's used |
|---|---|
| Formatting intelligence | Distinguishes statements, questions, and trailing speech via punctuation. |
| `keyterms_prompt` | Meeting-specific vocabulary: participant names, project names, technical terms. |
| `UpdateConfiguration` | Update keyterms mid-stream as new topics arise. |
| Higher `max_turn_silence` | 2000ms to allow meeting speakers time to think. |

**Turn detection config (balanced — wait for natural pauses):**

```json
{
  "speech_model": "u3-rt-pro",
  "min_end_of_turn_silence_when_confident": 560,
  "max_turn_silence": 2000
}
```

**Example keyterms:**
```python
["Alice Johnson", "Bob Smith", "Project Phoenix", "Q3 roadmap", "quarterly review", "action items", "deadline", "budget"]
```

---

## Tech Stack: Pipecat (Listen-Only Pipeline)

### Dependencies

```bash
pip install "pipecat-ai[assemblyai,cerebras,silero,daily,webrtc]" python-dotenv fastapi uvicorn pipecat-ai-small-webrtc-prebuilt
```

Note: No `rime` (TTS) needed — this is a listen-only agent.

Also download the Pipecat run helper file:

```bash
curl -O https://raw.githubusercontent.com/pipecat-ai/pipecat/9f223442c2799d22aac8a552c0af1d0ae7ff42c2/src/pipecat/examples/run.py
```

### API Keys Needed

- **AssemblyAI** — STT (`ASSEMBLYAI_API_KEY`)
- **Cerebras** — LLM for note generation (`CEREBRAS_API_KEY`)

### .env.example

```env
ASSEMBLYAI_API_KEY=your_assemblyai_api_key
CEREBRAS_API_KEY=your_cerebras_api_key
```

### Adaptation: Voice Agent Pipeline → Listen-Only Meeting Pipeline

Start from the Pipecat voice agent pattern but make these key changes:

1. **Trim the pipeline** — Remove TTS and `transport.output()` and `context_aggregator.assistant()`:
   ```python
   # Voice agent pipeline (full):
   pipeline = Pipeline([
       transport.input(),
       stt,
       context_aggregator.user(),
       llm,
       tts,
       transport.output(),
       context_aggregator.assistant(),
   ])

   # Meeting notes pipeline (listen-only):
   pipeline = Pipeline([
       transport.input(),
       stt,
       transcript_collector,  # Custom processor to collect turns
   ])
   ```

2. **Create a transcript collector processor** — A custom Pipecat processor that listens for finalized STT frames and buffers them.

3. **Adjust turn detection** — Use balanced config (560ms / 2000ms) instead of aggressive (100ms / 1200ms).

4. **Generate notes on disconnect** — When the client disconnects, send the buffered transcript to the LLM to produce structured notes.

### Conceptual Code Structure

```python
import argparse
import os
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.assemblyai.stt import AssemblyAISTTService, AssemblyAIConnectionParams
from pipecat.services.cerebras.llm import CerebrasLLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.services.daily import DailyParams

load_dotenv(override=True)

# Transcript buffer
transcript_buffer = []

transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=False,  # No audio output needed
        vad_analyzer=SileroVADAnalyzer(),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,
        vad_analyzer=SileroVADAnalyzer(),
    ),
}


class TranscriptCollector(FrameProcessor):
    """Custom processor that collects finalized transcript frames."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            transcript_buffer.append({
                "timestamp": datetime.now().isoformat(),
                "text": frame.text,
            })
            logger.info(f"[TRANSCRIPT] {frame.text}")
        await self.push_frame(frame, direction)


async def run_example(transport: BaseTransport, _: argparse.Namespace, handle_sigint: bool):
    stt = AssemblyAISTTService(
        api_key=os.getenv("ASSEMBLYAI_API_KEY"),
        vad_force_turn_endpoint=False,
        connection_params=AssemblyAIConnectionParams(
            min_end_of_turn_silence_when_confident=560,
            max_turn_silence=2000,
            keyterms_prompt=["Alice Johnson", "Bob Smith", "Project Phoenix", "Q3 roadmap"],
        )
    )

    transcript_collector = TranscriptCollector()

    # Listen-only pipeline: input → STT → collect transcripts
    pipeline = Pipeline([
        transport.input(),
        stt,
        transcript_collector,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=False,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — listening for meeting audio")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected — generating meeting notes")
        await generate_meeting_notes(transcript_buffer)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)


async def generate_meeting_notes(turns):
    """Send collected transcript to LLM for structured note generation."""
    if not turns:
        logger.warning("No transcript data to summarize")
        return

    transcript_text = "\n".join([f"[{t['timestamp']}] {t['text']}" for t in turns])

    # Call Cerebras LLM to generate structured notes
    # Output format: Summary, Key Decisions, Action Items, Next Steps
    logger.info(f"Generating notes from {len(turns)} transcript turns...")
    # Implementation: use httpx or the Cerebras SDK to call the LLM
    pass


if __name__ == "__main__":
    from pipecat.examples.run import main
    main(run_example, transport_params=transport_params)
```

**Important:** This is a conceptual structure. The actual frame types and processor API may differ — consult the Pipecat documentation for the correct frame types for receiving STT transcription results. The key architectural decisions are:
- Trimmed pipeline with no TTS or audio output
- Custom `TranscriptCollector` processor to buffer turns
- Note generation via LLM at session end

### How to Run

```bash
python meeting_notes.py
```

Open `http://localhost:7860` in your browser. Click "Connect" and speak — the agent will transcribe without responding.

---

## Deliverables Checklist

- [ ] `meeting_notes.py` — Working listen-only meeting agent
- [ ] `run.py` — Downloaded Pipecat run helper (via curl command in README)
- [ ] `.env.example` — Template with all required API keys
- [ ] `requirements.txt` — All Python dependencies
- [ ] `README.md` — Setup instructions, prerequisites, how to run, architecture overview, explanation of listen-only pipeline
- [ ] `guide.mdx` — Step-by-step documentation using `codefocussection` components

### guide.mdx Format

```jsx
<codefocussection
  filepath="meeting_notes.py"
  filerange="1-15"
  title="Import libraries and configure environment"
  themeColor="#0000FF"
  label="Server"
>
  Description of imports and setup.
</codefocussection>
```

Break the guide into: imports, transcript buffer & collector processor, STT config (balanced turn detection), listen-only pipeline assembly, note generation logic, and running the agent.

### Async-Only Note

Speaker diarization is not available in streaming. For speaker-labeled meeting notes, use a hybrid approach: stream during the meeting for live captions, then process the recording through the async API for diarized notes. Mention this in the README.
