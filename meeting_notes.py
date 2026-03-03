import argparse
import asyncio
import os
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    TranscriptionFrame,
    InterimTranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.assemblyai.stt import AssemblyAISTTService, AssemblyAIConnectionParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams

load_dotenv(override=True)

transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=False,
        vad_analyzer=SileroVADAnalyzer(),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                confidence=0.6,
                start_secs=0.1,
                stop_secs=0.8,
                min_volume=0.4,
            )
        ),
    ),
}

MEETING_NOTES_PROMPT = (
    "You are a meeting notes assistant. Given a transcript of a meeting, "
    "generate structured notes with the following sections:\n\n"
    "## Summary\nA brief overview of the meeting.\n\n"
    "## Key Discussion Points\nBulleted list of main topics discussed.\n\n"
    "## Decisions Made\nAny decisions reached during the meeting.\n\n"
    "## Action Items\nSpecific tasks assigned, with owners if mentioned.\n\n"
    "## Next Steps\nUpcoming deadlines, follow-ups, or scheduled meetings.\n\n"
    "Be concise and focus on the most important information."
)


class TranscriptCollector(FrameProcessor):
    """Collects finalized and partial transcript turns and broadcasts them via WebSocket."""

    def __init__(self):
        super().__init__()
        self.transcript_turns = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            turn = {
                "speaker": "user",
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "text": frame.text,
            }
            self.transcript_turns.append(turn)
            logger.info(f"[FINAL] [{turn['timestamp']}] {turn['text']}")
            from run import broadcast
            await broadcast({"type": "transcript", "data": turn})
        elif isinstance(frame, InterimTranscriptionFrame) and frame.text:
            logger.debug(f"[PARTIAL] {frame.text}")
            from run import broadcast
            await broadcast({"type": "partial", "data": {"text": frame.text}})
        await self.push_frame(frame, direction)


async def generate_meeting_notes(turns):
    """Send collected transcript to Cerebras LLM for structured note generation."""
    if not turns:
        logger.warning("No transcript data to summarize")
        return None

    transcript_text = "\n".join(
        [f"[{t['timestamp']}] {t['speaker'].upper()}: {t['text']}" for t in turns]
    )
    logger.info(f"Generating meeting notes from {len(turns)} transcript turns...")

    client = AsyncOpenAI(
        base_url="https://api.cerebras.ai/v1",
        api_key=os.getenv("CEREBRAS_API_KEY"),
    )

    max_retries = 4
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(
                model="llama3.1-8b",
                messages=[
                    {"role": "system", "content": MEETING_NOTES_PROMPT},
                    {
                        "role": "user",
                        "content": f"Generate structured meeting notes from this transcript:\n\n{transcript_text}",
                    },
                ],
                temperature=0.3,
                max_completion_tokens=2000,
            )

            notes = response.choices[0].message.content
            logger.info(f"\n{'='*60}\nMEETING NOTES\n{'='*60}\n{notes}\n{'='*60}")
            return notes
        except Exception as e:
            if "429" in str(e) and attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"Cerebras rate-limited (attempt {attempt + 1}/{max_retries}), retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Failed to generate meeting notes: {e}")
                return None


async def run_example(transport: BaseTransport, _: argparse.Namespace, handle_sigint: bool):
    logger.info("Starting listen-only meeting transcription agent")

    stt = AssemblyAISTTService(
        api_key=os.getenv("ASSEMBLYAI_API_KEY"),
        vad_force_turn_endpoint=True,
        connection_params=AssemblyAIConnectionParams(
            speech_model="universal-streaming-english",
            keyterms_prompt=[
                "action items", "next steps", "follow up", "deadline",
                "milestone", "deliverable", "stakeholder", "budget",
                "quarterly review", "roadmap", "AssemblyAI", "Universal-3 Pro",
            ],
        )
    )

    transcript_collector = TranscriptCollector()

    pipeline = Pipeline([
        transport.input(),
        stt,
        transcript_collector,
        transport.output(),
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
        logger.info("Client connected — listening for speech")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected — generating meeting notes")
        notes = await generate_meeting_notes(transcript_collector.transcript_turns)
        from run import broadcast
        if notes:
            await broadcast({"type": "notes", "data": notes})
        else:
            await broadcast({"type": "notes", "data": "**Meeting notes could not be generated.** The LLM provider returned an error — please try again shortly."})
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)


if __name__ == "__main__":
    from run import main

    main(run_example, transport_params=transport_params)
