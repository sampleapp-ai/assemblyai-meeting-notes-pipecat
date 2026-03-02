#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Mapping, Optional

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import IceServer, SmallWebRTCConnection
from pipecat.transports.services.daily import DailyParams, DailyTransport

# Load environment variables
load_dotenv(override=True)

# WebSocket broadcast infrastructure for transcript streaming
_websocket_clients: set = set()


async def broadcast(data: dict):
    """Broadcast a JSON message to all connected WebSocket clients."""
    disconnected = set()
    message = json.dumps(data)
    for ws in _websocket_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    _websocket_clients.difference_update(disconnected)


def get_transport_client_id(transport: BaseTransport, client: Any) -> str:
    if isinstance(transport, SmallWebRTCTransport):
        return client.pc_id
    elif isinstance(transport, DailyTransport):
        return client["id"]
    logger.warning(f"Unable to get client id from unsupported transport {type(transport)}")
    return ""


async def maybe_capture_participant_camera(
    transport: BaseTransport, client: Any, framerate: int = 0
):
    if isinstance(transport, DailyTransport):
        await transport.capture_participant_video(
            client["id"], framerate=framerate, video_source="camera"
        )


async def maybe_capture_participant_screen(
    transport: BaseTransport, client: Any, framerate: int = 0
):
    if isinstance(transport, DailyTransport):
        await transport.capture_participant_video(
            client["id"], framerate=framerate, video_source="screenVideo"
        )


def run_example_daily(
    run_example: Callable,
    args: argparse.Namespace,
    params: DailyParams,
):
    logger.info("Running example with DailyTransport...")

    from pipecat.examples.daily_runner import configure

    async def run():
        async with aiohttp.ClientSession() as session:
            (room_url, token) = await configure(session)

            # Run example function with DailyTransport transport arguments.
            transport = DailyTransport(room_url, token, "Pipecat", params=params)
            await run_example(transport, args, True)

    asyncio.run(run())


def run_example_webrtc(
    run_example: Callable,
    args: argparse.Namespace,
    params: TransportParams,
):
    logger.info("Running example with SmallWebRTCTransport...")

    app = FastAPI()

    # Store connections by pc_id
    pcs_map: Dict[str, SmallWebRTCConnection] = {}

    ice_servers = [
        IceServer(
            urls="stun:stun.l.google.com:19302",
        )
    ]

    # Add TURN server for production (WebRTC needs relay behind proxies like Fly.io)
    turn_url = os.getenv("TURN_URL")
    turn_username = os.getenv("TURN_USERNAME")
    turn_credential = os.getenv("TURN_CREDENTIAL")
    if turn_url:
        ice_servers.append(
            IceServer(
                urls=turn_url,
                username=turn_username or "",
                credential=turn_credential or "",
            )
        )

    # Mount custom frontend from client/ directory
    client_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client")
    app.mount("/client", StaticFiles(directory=client_dir, html=True), name="client")

    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/client/")

    @app.get("/api/ice-servers")
    async def get_ice_servers():
        servers = [{"urls": "stun:stun.l.google.com:19302"}]
        if turn_url:
            servers.append({
                "urls": turn_url,
                "username": turn_username or "",
                "credential": turn_credential or "",
            })
        return servers

    # WebSocket endpoint for streaming transcripts and notes to the frontend
    @app.websocket("/ws/transcripts")
    async def transcript_ws(websocket: WebSocket):
        await websocket.accept()
        _websocket_clients.add(websocket)
        logger.info("Transcript WebSocket client connected")
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            pass
        finally:
            _websocket_clients.discard(websocket)
            logger.info("Transcript WebSocket client disconnected")

    # Session-based endpoints for prebuilt UI v2.x
    sessions_map: Dict[str, SmallWebRTCConnection] = {}

    @app.post("/start")
    async def start():
        session_id = str(uuid.uuid4())
        logger.info(f"Starting new session: {session_id}")
        return {"session_id": session_id}

    @app.post("/sessions/{session_id}/api/offer")
    async def session_offer_post(session_id: str, request: Request, background_tasks: BackgroundTasks):
        body = await request.json()
        logger.info(f"POST session offer body keys: {list(body.keys())}")

        pipecat_connection = SmallWebRTCConnection(ice_servers)
        await pipecat_connection.initialize(sdp=body["sdp"], type=body["type"])

        @pipecat_connection.event_handler("closed")
        async def handle_disconnected(webrtc_connection: SmallWebRTCConnection):
            logger.info(f"Discarding peer connection for pc_id: {webrtc_connection.pc_id}")
            pcs_map.pop(webrtc_connection.pc_id, None)
            sessions_map.pop(session_id, None)

        transport = SmallWebRTCTransport(params=params, webrtc_connection=pipecat_connection)
        background_tasks.add_task(run_example, transport, args, False)

        answer = pipecat_connection.get_answer()
        pcs_map[answer["pc_id"]] = pipecat_connection
        sessions_map[session_id] = pipecat_connection

        return answer

    @app.patch("/sessions/{session_id}/api/offer")
    async def session_offer_patch(session_id: str, request: Request):
        body = await request.json()
        logger.info(f"PATCH session offer body keys: {list(body.keys())}")

        pc_id = body.get("pc_id")
        candidates = body.get("candidates", [])

        pipecat_connection = pcs_map.get(pc_id) or sessions_map.get(session_id)
        if not pipecat_connection:
            logger.error(f"No connection found for session {session_id}")
            return {"error": "No connection found"}

        for candidate in candidates:
            await pipecat_connection.add_ice_candidate(candidate)

        return {"status": "ok"}

    @app.post("/api/offer")
    async def offer(request: dict, background_tasks: BackgroundTasks):
        pc_id = request.get("pc_id")

        if pc_id and pc_id in pcs_map:
            pipecat_connection = pcs_map[pc_id]
            logger.info(f"Reusing existing connection for pc_id: {pc_id}")
            await pipecat_connection.renegotiate(
                sdp=request["sdp"],
                type=request["type"],
                restart_pc=request.get("restart_pc", False),
            )
        else:
            pipecat_connection = SmallWebRTCConnection(ice_servers)
            await pipecat_connection.initialize(sdp=request["sdp"], type=request["type"])

            @pipecat_connection.event_handler("closed")
            async def handle_disconnected(webrtc_connection: SmallWebRTCConnection):
                logger.info(f"Discarding peer connection for pc_id: {webrtc_connection.pc_id}")
                pcs_map.pop(webrtc_connection.pc_id, None)

            # Run example function with SmallWebRTC transport arguments.
            transport = SmallWebRTCTransport(params=params, webrtc_connection=pipecat_connection)
            background_tasks.add_task(run_example, transport, args, False)

        answer = pipecat_connection.get_answer()
        # Updating the peer connection inside the map
        pcs_map[answer["pc_id"]] = pipecat_connection

        return answer

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield  # Run app
        coros = [pc.close() for pc in pcs_map.values()]
        await asyncio.gather(*coros)
        pcs_map.clear()

    uvicorn.run(app, host=args.host, port=args.port)


def run_example_twilio(
    run_example: Callable,
    args: argparse.Namespace,
    params: FastAPIWebsocketParams,
):
    logger.info("Running example with FastAPIWebsocketTransport (Twilio)...")

    app = FastAPI()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins for testing
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/")
    async def start_call():
        logger.debug("POST TwiML")

        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{args.proxy}/ws"></Stream>
  </Connect>
  <Pause length="40"/>
</Response>
        """
        return HTMLResponse(content=xml_content, media_type="application/xml")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()

        logger.debug("WebSocket connection accepted")

        # Reading Twilio data.
        start_data = websocket.iter_text()
        await start_data.__anext__()
        call_data = json.loads(await start_data.__anext__())
        print(call_data, flush=True)
        stream_sid = call_data["start"]["streamSid"]
        call_sid = call_data["start"]["callSid"]

        # Create websocket transport and update params.
        params.add_wav_header = False
        params.serializer = TwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
        )
        transport = FastAPIWebsocketTransport(websocket=websocket, params=params)
        await run_example(transport, args, False)

    uvicorn.run(app, host=args.host, port=args.port)


def run_main(
    run_example: Callable,
    args: argparse.Namespace,
    transport_params: Mapping[str, Callable] = {},
):
    if args.transport not in transport_params:
        logger.error(f"Transport '{args.transport}' not supported by this example")
        return

    params = transport_params[args.transport]()
    match args.transport:
        case "daily":
            run_example_daily(run_example, args, params)
        case "webrtc":
            run_example_webrtc(run_example, args, params)
        case "twilio":
            run_example_twilio(run_example, args, params)


def main(
    run_example: Callable,
    *,
    parser: Optional[argparse.ArgumentParser] = None,
    transport_params: Mapping[str, Callable] = {},
):
    if not parser:
        parser = argparse.ArgumentParser(description="Pipecat Bot Runner")
    parser.add_argument(
        "--host", default="localhost", help="Host for HTTP server (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=7860, help="Port for HTTP server (default: 7860)"
    )
    parser.add_argument(
        "--transport",
        "-t",
        type=str,
        choices=["daily", "webrtc", "twilio"],
        default="webrtc",
        help="The transport this example should use",
    )
    parser.add_argument(
        "--proxy", "-x", help="A public proxy host name (no protocol, e.g. proxy.example.com)"
    )
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args()

    # Log level
    logger.remove(0)
    logger.add(sys.stderr, level="TRACE" if args.verbose else "DEBUG")

    # Import the bot file
    run_main(run_example, args, transport_params)
