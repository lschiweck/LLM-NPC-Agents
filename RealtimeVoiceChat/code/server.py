# server.py
from queue import Queue, Empty
import logging
from logsetup import setup_logging

setup_logging(logging.INFO)
logger = logging.getLogger(__name__)
if __name__ == "__main__":
    logger.info("🖥️👋 Welcome to local real-time voice chat")

from upsample_overlap import UpsampleOverlap
from datetime import datetime
from colors import Colors
import uvicorn
import asyncio
import struct
import json
import time
import threading # Keep threading for SpeechPipelineManager internals and AbortWorker
import sys
import os # Added for environment variable access
import re

from typing import Any, Dict, Optional, Callable, List
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import HTMLResponse, Response, FileResponse
from urllib.parse import parse_qs

USE_SSL = False
DEFAULT_ENGINE = os.getenv("DEFAULT_TTS_ENGINE", "kokoro")
DEFAULT_ORPHEUS_MODEL = os.getenv(
    "DEFAULT_ORPHEUS_MODEL",
    "orpheus-3b-0.1-ft-Q8_0-GGUF/orpheus-3b-0.1-ft-q8_0.gguf",
)
DEFAULT_LLM_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", "ollama")
DEFAULT_LLM_MODEL = os.getenv("DEFAULT_LLM_MODEL", "llama3")
DEFAULT_NO_THINK = os.getenv("DEFAULT_NO_THINK", "false").lower() == "true"

DIRECT_STREAM = DEFAULT_ENGINE == "orpheus"


@dataclass
class CharacterSession:
    character_id: str
    config: Dict[str, Any]
    pipeline: Optional["SpeechPipelineManager"] = None
    audio_input: Optional["AudioInputProcessor"] = None
    message_queue: Optional[asyncio.Queue] = None
    audio_queue: Optional[asyncio.Queue] = None
    callbacks: Optional['TranscriptionCallbacks'] = None
    tasks: List[asyncio.Task] = field(default_factory=list)
    upsampler: Optional[UpsampleOverlap] = None

    def stop_connection(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        self.tasks.clear()

        if self.audio_input:
            try:
                self.audio_input.shutdown()
            except Exception as exc:
                logger.warning(f"🖥️⚠️ Failed to shutdown audio input for {self.character_id}: {exc}")
        self.audio_input = None
        self.callbacks = None
        self.message_queue = None
        self.audio_queue = None
        self.upsampler = None

    def shutdown(self):
        self.stop_connection()
        if self.pipeline:
            try:
                self.pipeline.shutdown()
            except Exception as exc:
                logger.warning(f"🖥️⚠️ Failed to shutdown pipeline for {self.character_id}: {exc}")
            self.pipeline = None

if __name__ == "__main__":
    logger.info(f"🖥️⚙️ {Colors.apply('[PARAM]').blue} Starting engine: {Colors.apply(DEFAULT_ENGINE).blue}")
    logger.info(f"🖥️⚙️ {Colors.apply('[PARAM]').blue} Direct streaming: {Colors.apply('ON' if DIRECT_STREAM else 'OFF').blue}")

# Define the maximum allowed size for the incoming audio queue
try:
    MAX_AUDIO_QUEUE_SIZE = int(os.getenv("MAX_AUDIO_QUEUE_SIZE", 50))
    if __name__ == "__main__":
        logger.info(f"🖥️⚙️ {Colors.apply('[PARAM]').blue} Audio queue size limit set to: {Colors.apply(str(MAX_AUDIO_QUEUE_SIZE)).blue}")
except ValueError:
    if __name__ == "__main__":
        logger.warning("🖥️⚠️ Invalid MAX_AUDIO_QUEUE_SIZE env var. Using default: 50")
    MAX_AUDIO_QUEUE_SIZE = 50


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

#from handlerequests import LanguageProcessor
#from audio_out import AudioOutProcessor
from audio_in import AudioInputProcessor
from speech_pipeline_manager import SpeechPipelineManager
from game_manager import GameManager
import uvicorn
import asyncio
import struct
import json
import time
import threading # Keep threading for SpeechPipelineManager internals and AbortWorker
import sys
import os # Added for environment variable access
import re

LANGUAGE = "en"
# TTS_FINAL_TIMEOUT = 0.5 # unsure if 1.0 is needed for stability
TTS_FINAL_TIMEOUT = 1.0 # unsure if 1.0 is needed for stability

# --------------------------------------------------------------------
# Custom no-cache StaticFiles
# --------------------------------------------------------------------
class NoCacheStaticFiles(StaticFiles):
    """
    Serves static files without allowing client-side caching.

    Overrides the default Starlette StaticFiles to add 'Cache-Control' headers
    that prevent browsers from caching static assets. Useful for development.
    """
    async def get_response(self, path: str, scope: Dict[str, Any]) -> Response:
        """
        Gets the response for a requested path, adding no-cache headers.

        Args:
            path: The path to the static file requested.
            scope: The ASGI scope dictionary for the request.

        Returns:
            A Starlette Response object with cache-control headers modified.
        """
        response: Response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        # These might not be strictly necessary with no-store, but belt and suspenders
        if "etag" in response.headers:
             response.headers.__delitem__("etag")
        if "last-modified" in response.headers:
             response.headers.__delitem__("last-modified")
        return response

# --------------------------------------------------------------------
# Character configuration loading
# --------------------------------------------------------------------
def load_character_config() -> Dict[str, Dict[str, Any]]:
    config_path = Path(__file__).resolve().parent / "character_config.json"
    if not config_path.exists():
        logger.warning("🖥️⚠️ character_config.json not found; creating an empty template")
        config_path.write_text("{}", encoding="utf-8")
        return {}
    try:
        import json
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        logger.warning("🖥️⚠️ character_config.json does not contain a dict at top level")
    except Exception as exc:
        logger.warning(f"🖥️⚠️ Failed to load character_config.json: {exc}")
    return {}


# --------------------------------------------------------------------
# Lifespan management
# --------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the application's lifespan, initializing and shutting down resources.

    Initializes global components like SpeechPipelineManager, Upsampler, and
    AudioInputProcessor and stores them in `app.state`. Handles cleanup on shutdown.

    Args:
        app: The FastAPI application instance.
    """
    logger.info("🖥️▶️ Server starting up")
    # Initialize global components, not connection-specific state
    app.state.CharacterSessions: Dict[str, CharacterSession] = {}
    app.state.CharacterConfig = load_character_config()
    app.state.Aborting = False # Keep this? Its usage isn't clear in the provided snippet. Minimizing changes.
    
    # Initialize Game Manager - Step by step to find the bug
    app.state.GameManagerClients: List[WebSocket] = []
    
    # Step 1: Just create the GameManager object
    try:
        app.state.GameManager = GameManager()
        logger.info("🎮 Step 1: GameManager created OK")
    except Exception as e:
        logger.error(f"🎮💥 Step 1 FAILED: {e}")
        app.state.GameManager = None
    
    # Step 2: Set up callbacks (but don't start the loop yet)
    if app.state.GameManager:
        def get_character_histories() -> Dict[str, List[Dict]]:
            histories = {}
            sessions: Dict[str, CharacterSession] = app.state.CharacterSessions
            for char_id, session in sessions.items():
                if session.pipeline:
                    histories[char_id] = list(session.pipeline.history)
                else:
                    histories[char_id] = []
            return histories
        
        def on_gm_inject(target: str, instruction: str):
            sessions: Dict[str, CharacterSession] = app.state.CharacterSessions
            session = sessions.get(target)
            if session and session.pipeline:
                session.pipeline.inject(instruction)
                if session.message_queue:
                    asyncio.create_task(session.message_queue.put({
                        "type": "inject_confirmed",
                        "content": instruction,
                        "character_id": target,
                        "source": "GameManager"
                    }))
                logger.info(f"🎮💉 Game Manager injected into {target}")
            else:
                logger.warning(f"🎮⚠️ Cannot inject into {target}: session not found or no pipeline")
        
        app.state.GameManager.get_character_histories = get_character_histories
        app.state.GameManager.on_inject = on_gm_inject
        # Broadcast callback
        async def broadcast_gm_state(state: Dict):
            clients = app.state.GameManagerClients
            if not clients:
                return
            message = json.dumps({"type": "game_manager_state", **state})
            disconnected = []
            for client_ws in clients:
                try:
                    await client_ws.send_text(message)
                except Exception:
                    disconnected.append(client_ws)
            for client_ws in disconnected:
                if client_ws in clients:
                    clients.remove(client_ws)
        
        # Use a sync wrapper that creates a task for the async broadcast
        def sync_broadcast_wrapper(state: Dict):
            try:
                asyncio.create_task(broadcast_gm_state(state))
            except RuntimeError:
                # No event loop running yet - ignore
                pass
        
        app.state.GameManager.on_state_update = sync_broadcast_wrapper
        logger.info("🎮 Step 2: Callbacks set OK")
        
        # Start Game Manager loop - uses asyncio.create_task internally
        if app.state.GameManager.state.enabled:
            app.state.GameManager.start()
            logger.info("🎮 Step 3: Game Manager loop started")

    yield

    logger.info("🖥️⏹️ Server shutting down")
    
    # Stop Game Manager (if enabled)
    # if app.state.GameManager:
    #     app.state.GameManager.stop()
    
    sessions: Dict[str, CharacterSession] = getattr(app.state, "CharacterSessions", {})
    for session in sessions.values():
        session.shutdown()

# --------------------------------------------------------------------
# FastAPI app instance
# --------------------------------------------------------------------
app = FastAPI(lifespan=lifespan)

# Enable CORS if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files with no cache
app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")

@app.get("/favicon.ico")
async def favicon():
    """
    Serves the favicon.ico file.

    Returns:
        A FileResponse containing the favicon.
    """
    return FileResponse("static/favicon.ico")

@app.get("/")
async def get_index() -> HTMLResponse:
    """
    Serves the main index.html page.

    Reads the content of static/index.html and returns it as an HTML response.

    Returns:
        An HTMLResponse containing the content of index.html.
    """
    with open("static/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)

@app.get("/characters")
async def list_characters():
    return [
        {
            "id": cid,
            "name": config.get("display_name", cid)
        }
        for cid, config in app.state.CharacterConfig.items()
    ]

@app.get("/game_manager/status")
async def game_manager_status():
    """Get current Game Manager status."""
    gm: GameManager = app.state.GameManager
    return gm.get_state_for_ui()

@app.post("/game_manager/trigger")
async def game_manager_trigger():
    """Manually trigger a Game Manager tick."""
    gm: GameManager = app.state.GameManager
    if not gm.state.enabled:
        return {"error": "Game Manager is disabled"}
    gm.trigger_tick_now()
    return {"status": "triggered"}

@app.post("/game_manager/reload")
async def game_manager_reload():
    """Reload Game Manager configuration."""
    gm: GameManager = app.state.GameManager
    gm.reload_config()
    if gm.state.enabled and (gm._task is None or gm._task.done()):
        gm.start()
    return {"status": "reloaded", "enabled": gm.state.enabled}

@app.websocket("/ws/game_manager")
async def game_manager_websocket(ws: WebSocket):
    """WebSocket endpoint for Game Manager UI updates."""
    await ws.accept()
    app.state.GameManagerClients.append(ws)
    logger.info("🎮🔌 Game Manager client connected")
    
    # Send initial state
    gm: GameManager = app.state.GameManager
    if gm:
        await ws.send_json({"type": "game_manager_state", **gm.get_state_for_ui()})
    else:
        await ws.send_json({"type": "game_manager_state", "enabled": False})
    
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            
            if msg_type == "trigger" and gm:
                gm.trigger_tick_now()
            elif msg_type == "reload" and gm:
                gm.reload_config()
                await ws.send_json({"type": "game_manager_state", **gm.get_state_for_ui()})
            elif msg_type == "inject_clue" and gm:
                content = data.get("content", "").strip()
                if content:
                    gm.inject_clue(content)
                    await ws.send_json({"type": "game_manager_state", **gm.get_state_for_ui()})
            elif msg_type == "remove_clue" and gm:
                index = data.get("index")
                if index is not None:
                    gm.remove_clue(index)
                    await ws.send_json({"type": "game_manager_state", **gm.get_state_for_ui()})
    except Exception as e:
        logger.info(f"🎮🔌 Game Manager client disconnected: {e}")
    finally:
        if ws in app.state.GameManagerClients:
            app.state.GameManagerClients.remove(ws)

# --------------------------------------------------------------------
# Utility functions
# --------------------------------------------------------------------
def parse_json_message(text: str) -> dict:
    """
    Safely parses a JSON string into a dictionary.

    Logs a warning if the JSON is invalid and returns an empty dictionary.

    Args:
        text: The JSON string to parse.

    Returns:
        A dictionary representing the parsed JSON, or an empty dictionary on error.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("🖥️⚠️ Ignoring client message with invalid JSON")
        return {}

def format_timestamp_ns(timestamp_ns: int) -> str:
    """
    Formats a nanosecond timestamp into a human-readable HH:MM:SS.fff string.

    Args:
        timestamp_ns: The timestamp in nanoseconds since the epoch.

    Returns:
        A string formatted as hours:minutes:seconds.milliseconds.
    """
    # Split into whole seconds and the nanosecond remainder
    seconds = timestamp_ns // 1_000_000_000
    remainder_ns = timestamp_ns % 1_000_000_000

    # Convert seconds part into a datetime object (local time)
    dt = datetime.fromtimestamp(seconds)

    # Format the main time as HH:MM:SS
    time_str = dt.strftime("%H:%M:%S")

    # For instance, if you want milliseconds, divide the remainder by 1e6 and format as 3-digit
    milliseconds = remainder_ns // 1_000_000
    formatted_timestamp = f"{time_str}.{milliseconds:03d}"

    return formatted_timestamp

# --------------------------------------------------------------------
# WebSocket data processing
# --------------------------------------------------------------------

async def process_incoming_data(ws: WebSocket, session: CharacterSession) -> None:
    """
    Receives messages via WebSocket, processes audio and text messages.

    Handles binary audio chunks, extracting metadata (timestamp, flags) and
    putting the audio PCM data with metadata into the `incoming_chunks` queue.
    Applies back-pressure if the queue is full.
    Parses text messages (assumed JSON) and triggers actions based on message type
    (e.g., updates client TTS state via `callbacks`, clears history, sets speed).

    Args:
        ws: The WebSocket connection instance.
        app: The FastAPI application instance (for accessing global state if needed).
        incoming_chunks: An asyncio queue to put processed audio metadata dictionaries into.
        callbacks: The TranscriptionCallbacks instance for this connection to manage state.
    """
    try:
        while True:
            msg = await ws.receive()
            if "bytes" in msg and msg["bytes"]:
                raw = msg["bytes"]

                # Ensure we have at least an 8‑byte header: 4 bytes timestamp_ms + 4 bytes flags
                if len(raw) < 8:
                    logger.warning("🖥️⚠️ Received packet too short for 8‑byte header.")
                    continue

                # Unpack big‑endian uint32 timestamp (ms) and uint32 flags
                timestamp_ms, flags = struct.unpack("!II", raw[:8])
                client_sent_ns = timestamp_ms * 1_000_000

                # Build metadata using fixed fields
                metadata = {
                    "client_sent_ms":           timestamp_ms,
                    "client_sent":              client_sent_ns,
                    "client_sent_formatted":    format_timestamp_ns(client_sent_ns),
                    "isTTSPlaying":             bool(flags & 1),
                    "character_id":             session.character_id,
                }

                # Record server receive time
                server_ns = time.time_ns()
                metadata["server_received"] = server_ns
                metadata["server_received_formatted"] = format_timestamp_ns(server_ns)

                # The rest of the payload is raw PCM bytes
                metadata["pcm"] = raw[8:]

                # Check queue size before putting data
                current_qsize = session.audio_queue.qsize() if session.audio_queue else MAX_AUDIO_QUEUE_SIZE
                if current_qsize < MAX_AUDIO_QUEUE_SIZE:
                    # Now put only the metadata dict (containing PCM audio) into the processing queue.
                    await session.audio_queue.put(metadata) # type: ignore[arg-type]
                else:
                    # Queue is full, drop the chunk and log a warning
                    logger.warning(
                        f"🖥️⚠️ Audio queue full ({current_qsize}/{MAX_AUDIO_QUEUE_SIZE}); dropping chunk. Possible lag."
                    )

            elif "text" in msg and msg["text"]:
                # Text-based message: parse JSON
                data = parse_json_message(msg["text"])
                msg_type = data.get("type")
                logger.info(Colors.apply(f"🖥️📥 ←←Client[{session.character_id}]: {data}").orange)


                if msg_type == "tts_start":
                    logger.info("🖥️ℹ️ Received tts_start from client.")
                    # Update connection-specific state via callbacks
                    if session.callbacks:
                        session.callbacks.tts_client_playing = True
                elif msg_type == "tts_stop":
                    logger.info("🖥️ℹ️ Received tts_stop from client.")
                    # Update connection-specific state via callbacks
                    if session.callbacks:
                        session.callbacks.tts_client_playing = False
                # Add to the handleJSONMessage function in server.py
                elif msg_type == "clear_history":
                    logger.info("🖥️ℹ️ Received clear_history from client.")
                    session.pipeline.reset()
                elif msg_type == "set_speed":
                    speed_value = data.get("speed", 0)
                    speed_factor = speed_value / 100.0  # Convert 0-100 to 0.0-1.0
                    turn_detection = session.audio_input.transcriber.turn_detection
                    if turn_detection:
                        turn_detection.update_settings(speed_factor)
                        logger.info(f"🖥️⚙️ Updated turn detection settings to factor: {speed_factor:.2f}")
                elif msg_type == "inject":
                    # Inject context/instruction into the character's system prompt
                    content = data.get("content", "")
                    if content and session.pipeline:
                        injection = session.pipeline.inject(content)
                        # Send confirmation back to client
                        if session.message_queue:
                            await session.message_queue.put({
                                "type": "inject_confirmed",
                                "content": content,
                                "character_id": session.character_id
                            })
                        logger.info(f"🖥️💉 Injected into {session.character_id}: {content}")
                    else:
                        logger.warning(f"🖥️⚠️ Inject failed: empty content or no pipeline")


    except asyncio.CancelledError:
        pass # Task cancellation is expected on disconnect
    except WebSocketDisconnect as e:
        logger.warning(f"🖥️⚠️ {Colors.apply('WARNING').red} disconnect in process_incoming_data: {repr(e)}")
    except RuntimeError as e:  # Often raised on closed transports
        logger.error(f"🖥️💥 {Colors.apply('RUNTIME_ERROR').red} in process_incoming_data: {repr(e)}")
    except Exception as e:
        logger.exception(f"🖥️💥 {Colors.apply('EXCEPTION').red} in process_incoming_data: {repr(e)}")

async def send_text_messages(ws: WebSocket, session: CharacterSession) -> None:
    """
    Continuously sends text messages from a queue to the client via WebSocket.

    Waits for messages on the `message_queue`, formats them as JSON, and sends
    them to the connected WebSocket client. Logs non-TTS messages.

    Args:
        ws: The WebSocket connection instance.
        message_queue: An asyncio queue yielding dictionaries to be sent as JSON.
    """
    try:
        while True:
            await asyncio.sleep(0.001) # Yield control
            if session.message_queue is None:
                await asyncio.sleep(0.01)
                continue
            data = await session.message_queue.get()
            msg_type = data.get("type")
            if msg_type != "tts_chunk":
                logger.info(Colors.apply(f"🖥️📤 →→Client[{session.character_id}]: {data}").orange)
            data.setdefault("character_id", session.character_id)
            await ws.send_json(data)
    except asyncio.CancelledError:
        pass # Task cancellation is expected on disconnect
    except WebSocketDisconnect as e:
        logger.warning(f"🖥️⚠️ {Colors.apply('WARNING').red} disconnect in send_text_messages: {repr(e)}")
    except RuntimeError as e:  # Often raised on closed transports
        logger.error(f"🖥️💥 {Colors.apply('RUNTIME_ERROR').red} in send_text_messages: {repr(e)}")
    except Exception as e:
        logger.exception(f"🖥️💥 {Colors.apply('EXCEPTION').red} in send_text_messages: {repr(e)}")

async def _reset_interrupt_flag_async(audio_input: AudioInputProcessor, callbacks: 'TranscriptionCallbacks'):
    """
    Resets the microphone interruption flag after a delay (async version).

    Waits for 1 second, then checks if the AudioInputProcessor is still marked
    as interrupted. If so, resets the flag on both the processor and the
    connection-specific callbacks instance.

    Args:
        app: The FastAPI application instance (to access AudioInputProcessor).
        callbacks: The TranscriptionCallbacks instance for the connection.
    """
    await asyncio.sleep(1)
    # Check the AudioInputProcessor's own interrupted state
    if audio_input.interrupted:
        logger.info(f"{Colors.apply('🖥️🎙️ ▶️ Microphone continued (async reset)').cyan}")
        audio_input.interrupted = False
        # Reset connection-specific interruption time via callbacks
        callbacks.interruption_time = 0
        logger.info(Colors.apply("🖥️🎙️ interruption flag reset after TTS chunk (async)").cyan)

async def send_tts_chunks(app: FastAPI, session: CharacterSession) -> None:
    """
    Continuously sends TTS audio chunks from the SpeechPipelineManager to the client.

    Monitors the state of the current speech generation (if any) and the client
    connection (via `callbacks`). Retrieves audio chunks from the active generation's
    queue, upsamples/encodes them, and puts them onto the outgoing `message_queue`
    for the client. Handles the end-of-generation logic and state resets.

    Args:
        app: The FastAPI application instance (to access global components).
        message_queue: An asyncio queue to put outgoing TTS chunk messages onto.
        callbacks: The TranscriptionCallbacks instance managing this connection's state.
    """
    try:
        logger.info(f"🖥️🔊 Starting TTS chunk sender for {session.character_id}")
        last_quick_answer_chunk = 0
        last_chunk_sent = 0
        prev_status = None

        while True:
            await asyncio.sleep(0.001) # Yield control

            # Use connection-specific interruption_time via callbacks
            if (
                session.audio_input
                and session.audio_input.interrupted
                and session.callbacks
                and session.callbacks.interruption_time
                and time.time() - session.callbacks.interruption_time > 2.0
            ):
                session.audio_input.interrupted = False
                session.callbacks.interruption_time = 0 # Reset via callbacks
                logger.info(Colors.apply("🖥️🎙️ interruption flag reset after 2 seconds").cyan)

            is_tts_finished = session.pipeline is not None and session.pipeline.is_valid_gen() and session.pipeline.running_generation.audio_quick_finished

            def log_status():
                nonlocal prev_status
                last_quick_answer_chunk_decayed = (
                    last_quick_answer_chunk
                    and time.time() - last_quick_answer_chunk > TTS_FINAL_TIMEOUT
                    and time.time() - last_chunk_sent > TTS_FINAL_TIMEOUT
                )

                curr_status = (
                    # Access connection-specific state via callbacks
                    int(session.callbacks.tts_to_client if session.callbacks else 0),
                    int(session.callbacks.tts_client_playing if session.callbacks else 0),
                    int(session.callbacks.tts_chunk_sent if session.callbacks else 0),
                    1, # Placeholder?
                    int(session.callbacks.is_hot if session.callbacks else 0), # from callbacks
                    int(session.callbacks.synthesis_started if session.callbacks else 0), # from callbacks
                    int(session.pipeline.running_generation is not None), # session state
                    int(session.pipeline.is_valid_gen()), # session state
                    int(is_tts_finished), # Calculated local variable
                    int(session.audio_input.interrupted) # Input processor state
                )

                if curr_status != prev_status:
                    status = Colors.apply("🖥️🚦 State ").red
                    logger.info(
                        f"{status} ToClient {curr_status[0]}, "
                        f"ttsClientON {curr_status[1]}, " # Renamed slightly for clarity
                        f"ChunkSent {curr_status[2]}, "
                        f"hot {curr_status[4]}, synth {curr_status[5]}"
                        f" gen {curr_status[6]}"
                        f" valid {curr_status[7]}"
                        f" tts_q_fin {curr_status[8]}"
                        f" mic_inter {curr_status[9]}"
                    )
                    prev_status = curr_status

            # Use connection-specific state via callbacks
            if not session.callbacks or not session.callbacks.tts_to_client:
                await asyncio.sleep(0.001)
                log_status()
                continue

            if not session.pipeline or not session.pipeline.running_generation:
                await asyncio.sleep(0.001)
                log_status()
                continue

            if session.pipeline.running_generation.abortion_started:
                await asyncio.sleep(0.001)
                log_status()
                continue

            if not session.pipeline.running_generation.audio_quick_finished:
                session.pipeline.running_generation.tts_quick_allowed_event.set()

            if not session.pipeline.running_generation.quick_answer_first_chunk_ready:
                await asyncio.sleep(0.001)
                log_status()
                continue

            chunk = None
            try:
                chunk = session.pipeline.running_generation.audio_chunks.get_nowait()
                if chunk:
                    last_quick_answer_chunk = time.time()
            except Empty:
                final_expected = session.pipeline.running_generation.quick_answer_provided
                audio_final_finished = session.pipeline.running_generation.audio_final_finished

                if not final_expected or audio_final_finished:
                    logger.info("🖥️🏁 Sending of TTS chunks and 'user request/assistant answer' cycle finished.")
                    if session.callbacks:
                        session.callbacks.send_final_assistant_answer() # Callbacks method

                    assistant_answer = session.pipeline.running_generation.quick_answer + session.pipeline.running_generation.final_answer                    
                    session.pipeline.running_generation = None

                    if session.callbacks:
                        session.callbacks.tts_chunk_sent = False # Reset via callbacks
                        session.callbacks.reset_state() # Reset connection state via callbacks

                await asyncio.sleep(0.001)
                log_status()
                continue

            if session.upsampler is None:
                session.upsampler = UpsampleOverlap()
            base64_chunk = session.upsampler.get_base64_chunk(chunk)
            session.message_queue.put_nowait({
                "type": "tts_chunk",
                "content": base64_chunk
            })
            last_chunk_sent = time.time()

            # Use connection-specific state via callbacks
            if session.callbacks and not session.callbacks.tts_chunk_sent and session.audio_input:
                # Use the async helper function instead of a thread
                asyncio.create_task(_reset_interrupt_flag_async(session.audio_input, session.callbacks))

            if session.callbacks:
                session.callbacks.tts_chunk_sent = True # Set via callbacks

    except asyncio.CancelledError:
        pass # Task cancellation is expected on disconnect
    except WebSocketDisconnect as e:
        logger.warning(f"🖥️⚠️ {Colors.apply('WARNING').red} disconnect in send_tts_chunks: {repr(e)}")
    except RuntimeError as e:
        logger.error(f"🖥️💥 {Colors.apply('RUNTIME_ERROR').red} in send_tts_chunks: {repr(e)}")
    except Exception as e:
        logger.exception(f"🖥️💥 {Colors.apply('EXCEPTION').red} in send_tts_chunks: {repr(e)}")


# --------------------------------------------------------------------
# Callback class to handle transcription events
# --------------------------------------------------------------------
class TranscriptionCallbacks:
    """
    Manages state and callbacks for a single WebSocket connection's transcription lifecycle.

    This class holds connection-specific state flags (like TTS status, user interruption)
    and implements callback methods triggered by the `AudioInputProcessor` and
    `SpeechPipelineManager`. It sends messages back to the client via the provided
    `message_queue` and manages interaction logic like interruptions and final answer delivery.
    It also includes a threaded worker to handle abort checks based on partial transcription.
    """
    def __init__(self, session: CharacterSession):
        """
        Initializes the TranscriptionCallbacks instance for a WebSocket connection.

        Args:
            app: The FastAPI application instance (to access global components).
            message_queue: An asyncio queue for sending messages back to the client.
        """
        self.session = session
        if session.message_queue is None:
            session.message_queue = asyncio.Queue()
        self.message_queue = session.message_queue
        session.callbacks = self
        self.final_transcription = ""
        self.abort_text = ""
        self.last_abort_text = ""

        # Initialize connection-specific state flags here
        self.tts_to_client: bool = False
        self.user_interrupted: bool = False
        self.tts_chunk_sent: bool = False
        self.tts_client_playing: bool = False
        self.interruption_time: float = 0.0

        # These were already effectively instance variables or reset logic existed
        self.silence_active: bool = True
        self.is_hot: bool = False
        self.user_finished_turn: bool = False
        self.synthesis_started: bool = False
        self.assistant_answer: str = ""
        self.final_assistant_answer: str = ""
        self.is_processing_potential: bool = False
        self.is_processing_final: bool = False
        self.last_inferred_transcription: str = ""
        self.final_assistant_answer_sent: bool = False
        self.partial_transcription: str = "" # Added for clarity

        self.reset_state() # Call reset to ensure consistency

        self.abort_request_event = threading.Event()
        self.abort_worker_thread = threading.Thread(target=self._abort_worker, name="AbortWorker", daemon=True)
        self.abort_worker_thread.start()


    def reset_state(self):
        """Resets connection-specific state flags and variables to their initial values."""
        # Reset all connection-specific state flags
        self.tts_to_client = False
        self.user_interrupted = False
        self.tts_chunk_sent = False
        # Don't reset tts_client_playing here, it reflects client state reports
        self.interruption_time = 0.0

        # Reset other state variables
        self.silence_active = True
        self.is_hot = False
        self.user_finished_turn = False
        self.synthesis_started = False
        self.assistant_answer = ""
        self.final_assistant_answer = ""
        self.is_processing_potential = False
        self.is_processing_final = False
        self.last_inferred_transcription = ""
        self.final_assistant_answer_sent = False
        self.partial_transcription = ""

        # Keep the abort call related to the audio processor/pipeline manager
        if self.session.pipeline:
            self.session.pipeline.abort_generation()


    def _abort_worker(self):
        """Background thread worker to check for abort conditions based on partial text."""
        while True:
            was_set = self.abort_request_event.wait(timeout=0.1) # Check every 100ms
            if was_set:
                self.abort_request_event.clear()
                # Only trigger abort check if the text actually changed
                if self.last_abort_text != self.abort_text:
                    self.last_abort_text = self.abort_text
                    logger.debug(f"🖥️🧠 Abort check triggered by partial: '{self.abort_text}'")
                    if self.session.pipeline:
                        self.session.pipeline.check_abort(self.abort_text, False, "on_partial")

    def on_partial(self, txt: str):
        """
        Callback invoked when a partial transcription result is available.

        Updates internal state, sends the partial result to the client,
        and signals the abort worker thread to check for potential interruptions.

        Args:
            txt: The partial transcription text.
        """
        self.final_assistant_answer_sent = False # New user speech invalidates previous final answer sending state
        self.final_transcription = "" # Clear final transcription as this is partial
        self.partial_transcription = txt
        if self.message_queue:
            self.message_queue.put_nowait({"type": "partial_user_request", "content": txt})
        self.abort_text = txt # Update text used for abort check
        self.abort_request_event.set() # Signal the abort worker

    def safe_abort_running_syntheses(self, reason: str):
        """Placeholder for safely aborting syntheses (currently does nothing)."""
        # TODO: Implement actual abort logic if needed, potentially interacting with SpeechPipelineManager
        pass

    def on_tts_allowed_to_synthesize(self):
        """Callback invoked when the system determines TTS synthesis can proceed."""
        # Access global manager state
        if self.session.pipeline and self.session.pipeline.running_generation and not self.session.pipeline.running_generation.abortion_started:
            logger.info(f"{Colors.apply('🖥️🔊 TTS ALLOWED').blue}")
            self.session.pipeline.running_generation.tts_quick_allowed_event.set()

    def on_potential_sentence(self, txt: str):
        """
        Callback invoked when a potentially complete sentence is detected by the STT.

        Triggers the preparation of a speech generation based on this potential sentence.

        Args:
            txt: The potential sentence text.
        """
        logger.debug(f"🖥️🧠 Potential sentence: '{txt}'")
        # Access global manager state
        if self.session.pipeline:
            self.session.pipeline.prepare_generation(txt)

    def on_potential_final(self, txt: str):
        """
        Callback invoked when a potential *final* transcription is detected (hot state).

        Logs the potential final transcription.

        Args:
            txt: The potential final transcription text.
        """
        logger.info(f"{Colors.apply('🖥️🧠 HOT: ').magenta}{txt}")

    def on_potential_abort(self):
        """Callback invoked if the STT detects a potential need to abort based on user speech."""
        # Placeholder: Currently logs nothing, could trigger abort logic.
        pass

    def on_before_final(self, audio: bytes, txt: str):
        """
        Callback invoked just before the final STT result for a user turn is confirmed.

        Sets flags indicating user finished, allows TTS if pending, interrupts microphone input,
        releases TTS stream to client, sends final user request and any pending partial
        assistant answer to the client, and adds user request to history.

        Args:
            audio: The raw audio bytes corresponding to the final transcription. (Currently unused)
            txt: The transcription text (might be slightly refined in on_final).
        """
        logger.info(Colors.apply('🖥️🏁 =================== USER TURN END ===================').light_gray)
        self.user_finished_turn = True
        self.user_interrupted = False # Reset connection-specific flag (user finished, not interrupted)
        # Access global manager state
        if self.session.pipeline and self.session.pipeline.is_valid_gen():
            logger.info(f"{Colors.apply('🖥️🔊 TTS ALLOWED (before final)').blue}")
            self.session.pipeline.running_generation.tts_quick_allowed_event.set()

        # first block further incoming audio (Audio processor's state)
        if self.session.audio_input and not self.session.audio_input.interrupted:
            logger.info(f"{Colors.apply('🖥️🎙️ ⏸️ Microphone interrupted (end of turn)').cyan}")
            self.session.audio_input.interrupted = True
            self.interruption_time = time.time() # Set connection-specific flag

        logger.info(f"{Colors.apply('🖥️🔊 TTS STREAM RELEASED').blue}")
        self.tts_to_client = True # Set connection-specific flag

        # Send final user request (using the reliable final_transcription OR current partial if final isn't set yet)
        user_request_content = self.final_transcription if self.final_transcription else self.partial_transcription
        if self.message_queue:
            self.message_queue.put_nowait({
                "type": "final_user_request",
                "content": user_request_content
            })

        # Access global manager state
        if self.session.pipeline and self.session.pipeline.is_valid_gen():
            # Send partial assistant answer (if available) to the client
            # Use connection-specific user_interrupted flag
            if self.session.pipeline.running_generation.quick_answer and not self.user_interrupted and self.message_queue:
                self.assistant_answer = self.session.pipeline.running_generation.quick_answer
                self.message_queue.put_nowait({
                    "type": "partial_assistant_answer",
                    "content": self.assistant_answer
                })

        logger.info(f"🖥️🧠 Adding user request to history: '{user_request_content}'")
        # Access global manager state
        if self.session.pipeline:
            self.session.pipeline.history.append({"role": "user", "content": user_request_content})

    def on_final(self, txt: str):
        """
        Callback invoked when the final transcription result for a user turn is available.

        Logs the final transcription and stores it.

        Args:
            txt: The final transcription text.
        """
        logger.info(f"\n{Colors.apply('🖥️✅ FINAL USER REQUEST (STT Callback): ').green}{txt}")
        if not self.final_transcription: # Store it if not already set by on_before_final logic
             self.final_transcription = txt

    def abort_generations(self, reason: str):
        """
        Triggers the abortion of any ongoing speech generation process.

        Logs the reason and calls the SpeechPipelineManager's abort method.

        Args:
            reason: A string describing why the abortion is triggered.
        """
        logger.info(f"{Colors.apply('🖥️🛑 Aborting generation:').blue} {reason}")
        # Access global manager state
        if self.session.pipeline:
            self.session.pipeline.abort_generation(reason=f"server.py abort_generations: {reason}")

    def on_silence_active(self, silence_active: bool):
        """
        Callback invoked when the silence detection state changes.

        Updates the internal silence_active flag.

        Args:
            silence_active: True if silence is currently detected, False otherwise.
        """
        # logger.debug(f"🖥️🎙️ Silence active: {silence_active}") # Optional: Can be noisy
        self.silence_active = silence_active

    def on_partial_assistant_text(self, txt: str):
        """
        Callback invoked when a partial text result from the assistant (LLM) is available.

        Updates the internal assistant answer state and sends the partial answer to the client,
        unless the user has interrupted.

        Args:
            txt: The partial assistant text.
        """
        logger.info(f"{Colors.apply('🖥️💬 PARTIAL ASSISTANT ANSWER: ').green}{txt}")
        # Use connection-specific user_interrupted flag
        if not self.user_interrupted:
            self.assistant_answer = txt
            # Use connection-specific tts_to_client flag
            if self.tts_to_client and self.message_queue:
                self.message_queue.put_nowait({
                    "type": "partial_assistant_answer",
                    "content": txt
                })

    def on_recording_start(self):
        """
        Callback invoked when the audio input processor starts recording user speech.

        If client-side TTS is playing, it triggers an interruption: stops server-side
        TTS streaming, sends stop/interruption messages to the client, aborts ongoing
        generation, sends any final assistant answer generated so far, and resets relevant state.
        """
        logger.info(f"{Colors.ORANGE}🖥️🎙️ Recording started.{Colors.RESET} TTS Client Playing: {self.tts_client_playing}")
        # Use connection-specific tts_client_playing flag
        if self.tts_client_playing:
            self.tts_to_client = False # Stop server sending TTS
            self.user_interrupted = True # Mark connection as user interrupted
            logger.info(f"{Colors.apply('🖥️❗ INTERRUPTING TTS due to recording start').blue}")

            # Send final assistant answer *if* one was generated and not sent
            logger.info(Colors.apply("🖥️✅ Sending final assistant answer (forced on interruption)").pink)
            self.send_final_assistant_answer(forced=True)

            # Minimal reset for interruption:
            self.tts_chunk_sent = False # Reset chunk sending flag
            # self.assistant_answer = "" # Optional: Clear partial answer if needed

            logger.info(Colors.apply("🖥️🛑 Sending stop_tts to client."))
            if self.message_queue:
                self.message_queue.put_nowait({
                    "type": "stop_tts", # Client handles this to mute/ignore
                    "content": ""
                })

            logger.info(f"{Colors.apply('🖥️🛑 RECORDING START ABORTING GENERATION').red}")
            self.abort_generations("on_recording_start, user interrupts, TTS Playing")

            logger.info("🖥️❗ Sending tts_interruption to client.")
            if self.message_queue:
                self.message_queue.put_nowait({ # Tell client to stop playback and clear buffer
                    "type": "tts_interruption",
                    "content": ""
                })

            # Reset state *after* performing actions based on the old state
            # Be careful what exactly needs reset vs persists (like tts_client_playing)
            # self.reset_state() # Might clear too much, like user_interrupted prematurely

    def send_final_assistant_answer(self, forced=False):
        """
        Sends the final (or best available) assistant answer to the client.

        Constructs the full answer from quick and final parts if available.
        If `forced` and no full answer exists, uses the last partial answer.
        Cleans the text and sends it as 'final_assistant_answer' if not already sent.

        Args:
            forced: If True, attempts to send the last partial answer if no complete
                    final answer is available. Defaults to False.
        """
        final_answer = ""
        # Access global manager state
        if self.session.pipeline and self.session.pipeline.is_valid_gen():
            final_answer = self.session.pipeline.running_generation.quick_answer + self.session.pipeline.running_generation.final_answer

        if not final_answer: # Check if constructed answer is empty
            # If forced, try using the last known partial answer from this connection
            if forced and self.assistant_answer:
                 final_answer = self.assistant_answer
                 logger.warning(f"🖥️⚠️ Using partial answer as final (forced): '{final_answer}'")
            else:
                logger.warning(f"🖥️⚠️ Final assistant answer was empty, not sending.")
                return# Nothing to send

        logger.debug(f"🖥️✅ Attempting to send final answer: '{final_answer}' (Sent previously: {self.final_assistant_answer_sent})")

        if not self.final_assistant_answer_sent and final_answer:
            # Clean up the final answer text
            cleaned_answer = re.sub(r'[\r\n]+', ' ', final_answer)
            cleaned_answer = re.sub(r'\s+', ' ', cleaned_answer).strip()
            cleaned_answer = cleaned_answer.replace('\\n', ' ')
            cleaned_answer = re.sub(r'\s+', ' ', cleaned_answer).strip()

            if cleaned_answer: # Ensure it's not empty after cleaning
                logger.info(f"\n{Colors.apply('🖥️✅ FINAL ASSISTANT ANSWER (Sending): ').green}{cleaned_answer}")
                if self.message_queue:
                    self.message_queue.put_nowait({
                        "type": "final_assistant_answer",
                        "content": cleaned_answer
                    })
                if self.session.pipeline:
                    self.session.pipeline.history.append({"role": "assistant", "content": cleaned_answer})
                self.final_assistant_answer_sent = True
                self.final_assistant_answer = cleaned_answer # Store the sent answer
            else:
                logger.warning(f"🖥️⚠️ {Colors.YELLOW}Final assistant answer was empty after cleaning.{Colors.RESET}")
                self.final_assistant_answer_sent = False # Don't mark as sent
                self.final_assistant_answer = "" # Clear the stored answer
        elif forced and not final_answer: # Should not happen due to earlier check, but safety
             logger.warning(f"🖥️⚠️ {Colors.YELLOW}Forced send of final assistant answer, but it was empty.{Colors.RESET}")
             self.final_assistant_answer = "" # Clear the stored answer


# --------------------------------------------------------------------
# Main WebSocket endpoint
# --------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Handles the main WebSocket connection for real-time voice chat.

    Accepts a connection, sets up connection-specific state via `TranscriptionCallbacks`,
    initializes audio/message queues, and creates asyncio tasks for handling
    incoming data, audio processing, outgoing text messages, and outgoing TTS chunks.
    Manages the lifecycle of these tasks and cleans up on disconnect.

    Args:
        ws: The WebSocket connection instance provided by FastAPI.
    """
    logger.info("🖥️🔌 WebSocket /ws endpoint hit, accepting...")
    await ws.accept()
    logger.info("🖥️✅ Client connected via WebSocket.")

    params = parse_qs(ws.scope.get("query_string", b"").decode())
    character_id = params.get("characterId", [None])[0] or params.get("character_id", [None])[0]
    if not character_id:
        default_character = os.getenv("DEFAULT_CHARACTER_ID")
        if not default_character:
            config_keys = list(app.state.CharacterConfig.keys())
            if len(config_keys) == 1:
                default_character = config_keys[0]
        if default_character:
            logger.info(f"🖥️ℹ️ No characterId supplied; defaulting to {default_character}")
            character_id = default_character

    if not character_id:
        await ws.close(code=4400)
        logger.error("🖥️❌ Connection rejected: missing characterId in query string")
        return

    existing_session = app.state.CharacterSessions.get(character_id)
    if existing_session and existing_session.message_queue is not None:
        logger.warning(f"🖥️⚠️ Character {character_id} already has an active connection; dropping new attempt")
        await ws.close(code=4409)
        return

    config = app.state.CharacterConfig.get(character_id, {})
    session = existing_session or CharacterSession(character_id=character_id, config=config)
    app.state.CharacterSessions[character_id] = session

    if session.audio_input is None:
        session.audio_input = AudioInputProcessor(
            LANGUAGE,
            is_orpheus=(session.config.get("tts_engine", DEFAULT_ENGINE) == "orpheus"),
            pipeline_latency=0.5,
        )

    if session.pipeline is None:
        system_prompt = session.config.get("system_prompt")
        history = session.config.get("history", [])
        session.pipeline = SpeechPipelineManager(
            tts_engine=session.config.get("tts_engine", DEFAULT_ENGINE),
            llm_provider=session.config.get("llm_provider", DEFAULT_LLM_PROVIDER),
            llm_model=session.config.get("llm_model", DEFAULT_LLM_MODEL),
            no_think=session.config.get("no_think", DEFAULT_NO_THINK),
            orpheus_model=session.config.get("orpheus_model", DEFAULT_ORPHEUS_MODEL),
            system_prompt_override=system_prompt,
            history=history,
            voice=session.config.get("voice"),
            reference_audio=session.config.get("reference_audio"),
            session_id=character_id,
        )
    
    # Set up message queue
    message_queue = session.message_queue or asyncio.Queue()
    session.message_queue = message_queue

    audio_chunks = session.audio_queue or asyncio.Queue()
    session.audio_queue = audio_chunks

    callbacks = TranscriptionCallbacks(session)

    # Wire callbacks to the session-specific processors
    session.audio_input.realtime_callback = callbacks.on_partial
    session.audio_input.transcriber.potential_sentence_end = callbacks.on_potential_sentence
    session.audio_input.transcriber.on_tts_allowed_to_synthesize = callbacks.on_tts_allowed_to_synthesize
    session.audio_input.transcriber.potential_full_transcription_callback = callbacks.on_potential_final
    session.audio_input.transcriber.potential_full_transcription_abort_callback = callbacks.on_potential_abort
    session.audio_input.transcriber.full_transcription_callback = callbacks.on_final
    session.audio_input.transcriber.before_final_sentence = callbacks.on_before_final
    session.audio_input.recording_start_callback = callbacks.on_recording_start
    session.audio_input.silence_active_callback = callbacks.on_silence_active

    session.pipeline.on_partial_assistant_text = callbacks.on_partial_assistant_text

    tasks = [
        asyncio.create_task(process_incoming_data(ws, session)),
        asyncio.create_task(session.audio_input.process_chunk_queue(audio_chunks)),
        asyncio.create_task(send_text_messages(ws, session)),
        asyncio.create_task(send_tts_chunks(app, session)),
    ]
    session.tasks = tasks
    
    # Send character_ready signal - initialization is complete
    logger.info(f"🖥️✅ Character {character_id} fully initialized and ready")
    await ws.send_json({
        "type": "character_ready",
        "character_id": character_id
    })

    try:
        # Wait for any task to complete (e.g., client disconnect)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            if not task.done():
                task.cancel()
        # Await cancelled tasks to let them clean up if needed
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception as e:
        logger.error(f"🖥️💥 {Colors.apply('ERROR').red} in WebSocket session: {repr(e)}")
    finally:
        logger.info("🖥️🧹 Cleaning up WebSocket tasks...")
        for task in tasks:
            if not task.done():
                task.cancel()
        # Ensure all tasks are awaited after cancellation
        # Use return_exceptions=True to prevent gather from stopping on first error during cleanup
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("🖥️❌ WebSocket session ended.")

        session.stop_connection()
        if session.config.get("persist_history", False) and session.pipeline:
            session.config["history"] = session.pipeline.history
        else:
            session.pipeline.history.clear() if session.pipeline else None

# --------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------
if __name__ == "__main__":

    # Run the server without SSL
    if not USE_SSL:
        logger.info("🖥️▶️ Starting server without SSL.")
        uvicorn.run("server:app", host="0.0.0.0", port=8000, log_config=None)

    else:
        logger.info("🖥️🔒 Attempting to start server with SSL.")
        # Check if cert files exist
        cert_file = "127.0.0.1+1.pem"
        key_file = "127.0.0.1+1-key.pem"
        if not os.path.exists(cert_file) or not os.path.exists(key_file):
             logger.error(f"🖥️💥 SSL cert file ({cert_file}) or key file ({key_file}) not found.")
             logger.error("🖥️💥 Please generate them using mkcert:")
             logger.error("🖥️💥   choco install mkcert") # Assuming Windows based on earlier check, adjust if needed
             logger.error("🖥️💥   mkcert -install")
             logger.error("🖥️💥   mkcert 127.0.0.1 YOUR_LOCAL_IP") # Remind user to replace with actual IP if needed
             logger.error("🖥️💥 Exiting.")
             sys.exit(1)

        # Run the server with SSL
        logger.info(f"🖥️▶️ Starting server with SSL (cert: {cert_file}, key: {key_file}).")
        uvicorn.run(
            "server:app",
            host="0.0.0.0",
            port=8000,
            log_config=None,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
