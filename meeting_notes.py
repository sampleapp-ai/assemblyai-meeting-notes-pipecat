#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import os
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    TranscriptionFrame,
    InterimTranscriptionFrame,
    TextFrame,
    LLMFullResponseEndFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.assemblyai.stt import AssemblyAISTTService, AssemblyAIConnectionParams
from pipecat.services.cerebras.llm import CerebrasLLMService
from pipecat.services.rime.tts import RimeTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.daily.transport import DailyParams

load_dotenv(override=True)

transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
}

SYSTEM_INSTRUCTIONS = (
    "You are a helpful AI meeting facilitator. You participate in meetings by "
    "listening to discussion, asking clarifying questions, and helping keep the "
    "conversation on track.\n\n"
    "Your role:\n"
    "- Help summarize discussion points when asked\n"
    "- Ask clarifying questions if something is ambiguous\n"
    "- Remind participants of agenda items or time constraints\n"
    "- Offer to capture action items and decisions\n"
    "- Keep responses concise — you're in a live meeting, not writing an essay\n\n"
    "Keep your responses short and conversational. Your output will be converted "
    "to audio so don't include special characters, markdown formatting, or long "
    "lists in your answers. Speak naturally as a meeting participant would."
)

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
    """Collects finalized user transcript turns and broadcasts them via WebSocket."""

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
            logger.info(f"[USER] [{turn['timestamp']}] {turn['text']}")
            from run import broadcast
            await broadcast({"type": "transcript", "data": turn})
        elif isinstance(frame, InterimTranscriptionFrame) and frame.text:
            from run import broadcast
            await broadcast({"type": "partial", "data": {"text": frame.text}})
        await self.push_frame(frame, direction)


class AgentResponseCollector(FrameProcessor):
    """Collects agent LLM response text chunks and broadcasts complete responses."""

    def __init__(self, transcript_collector: TranscriptCollector):
        super().__init__()
        self._buffer = ""
        self._transcript_collector = transcript_collector

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame) and frame.text:
            self._buffer += frame.text
        elif isinstance(frame, LLMFullResponseEndFrame) and self._buffer:
            turn = {
                "speaker": "agent",
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "text": self._buffer.strip(),
            }
            self._transcript_collector.transcript_turns.append(turn)
            logger.info(f"[AGENT] [{turn['timestamp']}] {turn['text']}")
            from run import broadcast
            await broadcast({"type": "transcript", "data": turn})
            self._buffer = ""
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


async def run_example(transport: BaseTransport, _: argparse.Namespace, handle_sigint: bool):
    logger.info("Starting meeting facilitator agent")

    stt = AssemblyAISTTService(
        api_key=os.getenv("ASSEMBLYAI_API_KEY"),
        vad_force_turn_endpoint=False,
        connection_params=AssemblyAIConnectionParams(
            min_end_of_turn_silence_when_confident=560,
            max_turn_silence=2000,
            keyterms_prompt=[
                "action items", "next steps", "follow up", "deadline",
                "milestone", "deliverable", "stakeholder", "budget",
                "quarterly review", "roadmap", "AssemblyAI", "Universal-3 Pro",
            ],
        )
    )

    tts = RimeTTSService(
        api_key=os.getenv("RIME_API_KEY"),
        voice_id="astra",
        model="mistv2",
    )

    llm = CerebrasLLMService(
        api_key=os.getenv("CEREBRAS_API_KEY"),
        model="llama3.1-8b",
        params=CerebrasLLMService.InputParams(
            temperature=0.7,
            max_completion_tokens=1000,
        )
    )

    messages = [{"role": "system", "content": SYSTEM_INSTRUCTIONS}]
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    transcript_collector = TranscriptCollector()
    agent_response_collector = AgentResponseCollector(transcript_collector)

    pipeline = Pipeline([
        transport.input(),
        stt,
        transcript_collector,
        context_aggregator.user(),
        llm,
        agent_response_collector,
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — meeting facilitator ready")
        messages.append({
            "role": "system",
            "content": (
                "Briefly introduce yourself as an AI meeting facilitator. "
                "Say you're here to help keep the meeting on track and capture key points. "
                "Ask what's on the agenda today."
            ),
        })
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected — generating meeting notes")
        notes = await generate_meeting_notes(transcript_collector.transcript_turns)
        if notes:
            from run import broadcast
            await broadcast({"type": "notes", "data": notes})
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)


if __name__ == "__main__":
    from run import main

    main(run_example, transport_params=transport_params)
