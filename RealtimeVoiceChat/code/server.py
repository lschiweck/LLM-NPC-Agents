# server.py
from queue import Queue, Empty
from collections import deque
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

# Load centralized config
from server_config_loader import load_server_config, get_server_config
from conversation_logger import init_conversation_logger, get_conversation_logger

# Load config at module level
_server_config = load_server_config()

# Apply config values (with env var overrides for backwards compatibility)
USE_SSL = _server_config.server.use_ssl
DEFAULT_ENGINE = os.getenv("DEFAULT_TTS_ENGINE", _server_config.defaults.tts_engine)
DEFAULT_ORPHEUS_MODEL = os.getenv(
    "DEFAULT_ORPHEUS_MODEL",
    _server_config.defaults.orpheus_model,
)
DEFAULT_LLM_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", _server_config.defaults.llm_provider)
DEFAULT_LLM_MODEL = os.getenv("DEFAULT_LLM_MODEL", _server_config.defaults.llm_model)
DEFAULT_NO_THINK = os.getenv("DEFAULT_NO_THINK", str(_server_config.defaults.no_think)).lower() == "true"
LANGUAGE = _server_config.defaults.language

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
    uses_shared_audio: bool = False

    def stop_connection(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        self.tasks.clear()

        # IMPORTANT:
        # Do NOT shutdown and discard AudioInputProcessor on disconnect.
        # It is expensive to initialize (Whisper/VAD/turn detection), and tearing it down
        # causes long "it feels bugged" stalls on reconnects (as seen in logs ~17s).
        #
        # Instead: detach callbacks and leave the processor warm. Full shutdown still happens
        # in CharacterSession.shutdown() on server shutdown.
        if self.audio_input and not self.uses_shared_audio:
            try:
                self.audio_input.realtime_callback = None
                self.audio_input.recording_start_callback = None
                self.audio_input.silence_active_callback = None
                self.audio_input.interrupted = False
                self.audio_input.last_partial_text = None
            except Exception as exc:
                logger.warning(f"🖥️⚠️ Failed to detach audio callbacks for {self.character_id}: {exc}")
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
from npc_conversation import NPCConversationOrchestrator, NPCConversationConfig, ConversationTurn
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


def load_story_bible() -> str:
    """
    Load the shared canon story bible that all NPCs should follow.
    This is prepended to each character's game_knowledge (Layer 3) so both
    player↔NPC and NPC↔NPC conversations stay coherent and on-topic.
    """
    story_path = Path(__file__).resolve().parent / "story_bible.txt"
    try:
        if story_path.exists():
            text = story_path.read_text(encoding="utf-8").strip()
            if text:
                logger.info(f"📚 Loaded story bible from {story_path.name} ({len(text)} chars)")
                return text
    except Exception as exc:
        logger.warning(f"📚⚠️ Failed to read story bible: {exc}")
    return ""


def combine_game_knowledge(story_bible: str, character_game_knowledge: Optional[str]) -> Optional[str]:
    """
    Combine shared canon story bible with per-character knowledge.
    Kept deliberately simple to avoid breaking the prompt layering.
    """
    sb = (story_bible or "").strip()
    ck = (character_game_knowledge or "").strip()
    if sb and ck:
        return f"{sb}\n\n--- CHARACTER-SPECIFIC FACTS ---\n{ck}".strip()
    if sb:
        return sb
    if ck:
        return ck
    return None


def audio_input_needs_recreate(audio_input: Optional["AudioInputProcessor"]) -> bool:
    """
    Determine whether the AudioInputProcessor should be recreated.
    We keep it warm across disconnects, but if its background transcription task
    has died or it marked itself as failed, reusing it will make the character
    feel "bugged" forever until a server restart.
    """
    if audio_input is None:
        return True
    if getattr(audio_input, "_transcription_failed", False):
        return True
    task = getattr(audio_input, "transcription_task", None)
    if task is None:
        return True
    try:
        if task.done():
            return True
    except Exception:
        # If anything about task state is weird, recreate.
        return True
    return False


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

    # Store the main asyncio loop so background threads (audio processing) can safely schedule work.
    # Many callbacks (e.g. on_recording_start) are triggered from asyncio.to_thread(...) and thus
    # do NOT have a running event loop.
    app.state.main_loop = asyncio.get_running_loop()

    # Shared canon story bible (used to keep all NPCs coherent and on-topic)
    app.state.StoryBible = load_story_bible()

    def _schedule_on_main_loop(coro):
        """
        Schedule an async coroutine to run on the FastAPI main event loop, from any thread.
        """
        loop = getattr(app.state, "main_loop", None)
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
            if running is loop:
                asyncio.create_task(coro)
                return
        except RuntimeError:
            # Not in an event loop (likely a background thread)
            pass
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            # Loop may be closing during shutdown; ignore.
            pass
    
    # Initialize conversation logger
    config = get_server_config()
    conv_logger = init_conversation_logger(config.logging.__dict__)
    app.state.ConversationLogger = conv_logger
    logger.info(f"📝 Conversation logging: {'ENABLED' if conv_logger.enabled else 'DISABLED'}")
    
    # Initialize global components, not connection-specific state
    app.state.CharacterSessions: Dict[str, CharacterSession] = {}
    app.state.CharacterConfig = load_character_config()
    app.state.Aborting = False # Keep this? Its usage isn't clear in the provided snippet. Minimizing changes.

    # Shared STT pipeline (single GPU Whisper/TurnDetect for all characters)
    app.state.SharedSttEnabled = bool(config.initialization.shared_stt_pipeline)
    app.state.SharedAudioInput = None
    app.state.SharedAudioQueue = None
    app.state.SharedAudioTask = None
    app.state.SharedAudioSeen = None
    app.state.SharedAudioSeenSet = None
    app.state.SharedAudioSeenMax = int(getattr(config.initialization, "shared_stt_dedupe_window", 200))
    # Track which character is the active conversation target (receives audio most recently)
    # Only this character should generate LLM responses when using shared STT
    app.state.ActiveConversationTarget = None
    app.state.ActiveConversationTargetTime = 0.0

    if app.state.SharedSttEnabled:
        any_orpheus = any(
            (cfg.get("tts_engine", DEFAULT_ENGINE) == "orpheus")
            for cfg in (app.state.CharacterConfig or {}).values()
        )
        app.state.SharedAudioInput = AudioInputProcessor(
            LANGUAGE,
            is_orpheus=any_orpheus,
            pipeline_latency=0.5,
        )
        app.state.SharedAudioQueue = asyncio.Queue()
        app.state.SharedAudioTask = asyncio.create_task(
            app.state.SharedAudioInput.process_chunk_queue(app.state.SharedAudioQueue)
        )
        app.state.SharedAudioSeen = deque()
        app.state.SharedAudioSeenSet = set()
        logger.info("🎙️ Shared STT pipeline enabled (single GPU STT for all characters)")
    
    # Pre-initialize pipelines if configured
    pre_init_mode = config.initialization.pre_init_mode
    if config.initialization.pre_init_pipelines and pre_init_mode != "none":
        if pre_init_mode == "all":
            # Load all characters from character_config.json
            pre_init_ids = list(app.state.CharacterConfig.keys())
            logger.info(f"🚀 Pre-init mode: ALL - will load all {len(pre_init_ids)} characters")
        elif pre_init_mode == "specific":
            # Only load specified character IDs
            pre_init_ids = config.initialization.pre_init_character_ids
            logger.info(f"🚀 Pre-init mode: SPECIFIC - will load {len(pre_init_ids)} characters")
        else:
            pre_init_ids = []
            logger.warning(f"🚀⚠️ Unknown pre_init_mode '{pre_init_mode}', skipping pre-initialization")
        
        if pre_init_ids:
            logger.info(f"🚀 Pre-initializing pipelines for: {pre_init_ids}")
            for char_id in pre_init_ids:
                char_config = app.state.CharacterConfig.get(char_id, {})
                if not char_config:
                    logger.warning(f"🚀⚠️ Character {char_id} not found in config, skipping")
                    continue
                    
                logger.info(f"🚀 Pre-initializing {char_id}...")
                try:
                    # Create session with pipeline
                    session = CharacterSession(character_id=char_id, config=char_config)
                    
                    # Extract prompt layers
                    personality = char_config.get("personality")
                    game_knowledge = combine_game_knowledge(
                        getattr(app.state, "StoryBible", ""),
                        char_config.get("game_knowledge"),
                    )
                    system_prompt = char_config.get("system_prompt") if not personality and not game_knowledge else None
                    
                    # Create pipeline (this loads TTS/LLM models)
                    session.pipeline = SpeechPipelineManager(
                        tts_engine=char_config.get("tts_engine", DEFAULT_ENGINE),
                        llm_provider=char_config.get("llm_provider", DEFAULT_LLM_PROVIDER),
                        llm_model=char_config.get("llm_model", DEFAULT_LLM_MODEL),
                        no_think=char_config.get("no_think", DEFAULT_NO_THINK),
                        orpheus_model=char_config.get("orpheus_model", DEFAULT_ORPHEUS_MODEL),
                        personality=personality,
                        game_knowledge=game_knowledge,
                        system_prompt_override=system_prompt,
                        history=char_config.get("history", []),
                        voice=char_config.get("voice"),
                        reference_audio=char_config.get("reference_audio"),
                        session_id=char_id,
                    )
                    
                    # Also pre-initialize AudioInputProcessor (loads Whisper, VAD, turn detection)
                    if app.state.SharedSttEnabled:
                        session.audio_input = app.state.SharedAudioInput
                        session.uses_shared_audio = True
                        logger.info(f"🚀 Using shared STT pipeline for {char_id}")
                    else:
                        logger.info(f"🚀 Pre-initializing AudioInputProcessor for {char_id}...")
                        session.audio_input = AudioInputProcessor(
                            LANGUAGE,
                            is_orpheus=(char_config.get("tts_engine", DEFAULT_ENGINE) == "orpheus"),
                            pipeline_latency=0.5,
                        )
                    
                    app.state.CharacterSessions[char_id] = session
                    logger.info(f"🚀✅ Pre-initialized {char_id} (pipeline + audio)")
                except Exception as e:
                    logger.error(f"🚀💥 Failed to pre-initialize {char_id}: {e}")
            
            logger.info(f"🚀 Pre-initialization complete. {len(app.state.CharacterSessions)} characters ready.")
            # Log what's actually in CharacterSessions
            for cid, sess in app.state.CharacterSessions.items():
                logger.info(f"🚀📋 CharacterSessions['{cid}']: pipeline={'✅' if sess.pipeline else '❌'}, audio_input={'✅' if sess.audio_input else '❌'}")
    
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
                
                # Log injection
                conv_logger = get_conversation_logger()
                if conv_logger:
                    conv_logger.log_injection(target, instruction, source="game_manager")
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
            _schedule_on_main_loop(broadcast_gm_state(state))
        
        app.state.GameManager.on_state_update = sync_broadcast_wrapper
        logger.info("🎮 Step 2: Callbacks set OK")
        
        # Start Game Manager loop - uses asyncio.create_task internally
        if app.state.GameManager.state.enabled:
            app.state.GameManager.start()
            logger.info("🎮 Step 3: Game Manager loop started")

    # Initialize NPC Conversation Orchestrator
    app.state.NPCConversationClients: List[WebSocket] = []
    
    def get_npc_pipeline(character_id: str):
        sessions: Dict[str, CharacterSession] = app.state.CharacterSessions
        session = sessions.get(character_id)
        if not session:
            logger.warning(f"🎭 No session found for {character_id}. Available: {list(sessions.keys())}")
            return None
        if not session.pipeline:
            logger.warning(f"🎭 Session exists for {character_id} but pipeline not initialized")
            return None
        return session.pipeline
    
    async def broadcast_npc_conv_state(state):
        clients = app.state.NPCConversationClients
        if not clients:
            return
        message = json.dumps({"type": "npc_conversation_state", **state.to_dict()})
        disconnected = []
        for client_ws in clients:
            try:
                await client_ws.send_text(message)
            except Exception:
                disconnected.append(client_ws)
        for client_ws in disconnected:
            if client_ws in clients:
                clients.remove(client_ws)
    
    def on_npc_state_update(state):
        _schedule_on_main_loop(broadcast_npc_conv_state(state))
    
    async def broadcast_npc_turn(turn: ConversationTurn, audio_bytes: bytes):
        clients = app.state.NPCConversationClients
        if not clients:
            logger.warning("🎭 No NPC conversation clients connected to receive turn")
            return
        
        logger.info(f"🎭 Broadcasting turn {turn.turn_number} from {turn.speaker_id} to {len(clients)} clients")
        
        # Send turn info as JSON first
        turn_msg = json.dumps({
            "type": "npc_conversation_turn",
            "speaker_id": turn.speaker_id,
            "message": turn.message,
            "turn_number": turn.turn_number,
            "is_last": turn.is_last
        })
        
        # Send audio if available
        audio_msg = None
        if audio_bytes and len(audio_bytes) > 0:
            # Prefix with speaker ID for client to identify
            header = turn.speaker_id.encode('utf-8').ljust(32, b'\x00')
            audio_msg = header + audio_bytes
            logger.info(f"🎭 Sending {len(audio_bytes)} bytes of audio for {turn.speaker_id}")
        else:
            logger.warning(f"🎭 No audio data for {turn.speaker_id}")
        
        disconnected = []
        for client_ws in clients:
            try:
                await client_ws.send_text(turn_msg)
                if audio_msg:
                    await client_ws.send_bytes(audio_msg)
            except Exception as e:
                logger.error(f"🎭 Failed to send to client: {e}")
                disconnected.append(client_ws)
        for client_ws in disconnected:
            if client_ws in clients:
                clients.remove(client_ws)
    
    def on_npc_turn_complete(turn: ConversationTurn, audio_bytes: bytes):
        _schedule_on_main_loop(broadcast_npc_turn(turn, audio_bytes))
    
    async def broadcast_npc_conversation_interrupted(reason: str = "player_speaking"):
        """Broadcast interruption message to all NPC conversation clients - tells them to stop playback."""
        clients = app.state.NPCConversationClients
        if not clients:
            return
        
        logger.info(f"🎭🛑 Broadcasting conversation interrupted to {len(clients)} clients (reason={reason})")
        
        message = json.dumps({
            "type": "npc_conversation_interrupted",
            "reason": reason
        })
        
        disconnected = []
        for client_ws in clients:
            try:
                await client_ws.send_text(message)
            except Exception as e:
                logger.error(f"🎭 Failed to send interrupt message to client: {e}")
                disconnected.append(client_ws)
        for client_ws in disconnected:
            if client_ws in clients:
                clients.remove(client_ws)
    
    def on_npc_conversation_interrupted():
        """Sync callback wrapper for conversation interruption."""
        _schedule_on_main_loop(broadcast_npc_conversation_interrupted(reason="player_speaking"))

    # Store for use from other callbacks (some of which run in background threads)
    app.state.broadcast_npc_conversation_interrupted = broadcast_npc_conversation_interrupted
    
    app.state.NPCConversation = NPCConversationOrchestrator(
        get_pipeline=get_npc_pipeline,
        on_turn_complete=on_npc_turn_complete,
        on_state_update=on_npc_state_update,
        on_conversation_interrupted=on_npc_conversation_interrupted
    )
    logger.info("🎭 NPC Conversation Orchestrator initialized")
    
    # Helper to broadcast character connection updates to NPC conversation clients
    async def broadcast_npc_character_update():
        clients = app.state.NPCConversationClients
        if not clients:
            return
        config = app.state.CharacterConfig
        all_characters = list(config.keys()) if config else []
        sessions: Dict[str, CharacterSession] = app.state.CharacterSessions
        connected_characters = [cid for cid, session in sessions.items() if session.pipeline]
        
        message = json.dumps({
            "type": "available_characters",
            "characters": all_characters,
            "connected": connected_characters
        })
        disconnected = []
        for client_ws in clients:
            try:
                await client_ws.send_text(message)
            except Exception:
                disconnected.append(client_ws)
        for client_ws in disconnected:
            if client_ws in clients:
                clients.remove(client_ws)
    
    # Store the broadcast function for use elsewhere
    app.state.broadcast_npc_character_update = broadcast_npc_character_update

    yield

    logger.info("🖥️⏹️ Server shutting down")
    
    # Close conversation logger (ensures all logs are flushed)
    if hasattr(app.state, 'ConversationLogger') and app.state.ConversationLogger:
        app.state.ConversationLogger.close()
    
    # Stop Game Manager (if enabled)
    # if app.state.GameManager:
    #     app.state.GameManager.stop()

    # Shutdown shared STT pipeline if enabled
    shared_task = getattr(app.state, "SharedAudioTask", None)
    if shared_task is not None and not shared_task.done():
        shared_task.cancel()
        await asyncio.gather(shared_task, return_exceptions=True)
    shared_audio = getattr(app.state, "SharedAudioInput", None)
    if shared_audio is not None:
        shared_audio.shutdown()
    
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

@app.get("/npc_injection_config")
async def get_npc_injection_config():
    """Get the NPC injection trigger configuration."""
    import json
    config_path = Path(__file__).parent / "npc_conversation_injections.json"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        return {"error": "Config file not found"}

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


@app.websocket("/ws/npc_conversation")
async def npc_conversation_websocket(ws: WebSocket):
    """WebSocket endpoint for NPC-to-NPC conversations."""
    await ws.accept()
    app.state.NPCConversationClients.append(ws)
    logger.info("🎭🔌 NPC Conversation client connected")
    
    # Send initial state
    orchestrator: NPCConversationOrchestrator = app.state.NPCConversation
    if orchestrator:
        await ws.send_json({"type": "npc_conversation_state", **orchestrator.get_state().to_dict()})
    
    # Send available characters (from config)
    config = app.state.CharacterConfig
    all_characters = list(config.keys()) if config else []
    
    # Also send which characters are currently connected (have active sessions with pipelines)
    sessions: Dict[str, CharacterSession] = app.state.CharacterSessions
    connected_characters = [cid for cid, session in sessions.items() if session.pipeline]
    
    await ws.send_json({
        "type": "available_characters", 
        "characters": all_characters,
        "connected": connected_characters
    })
    logger.info(f"🎭 NPC Conv client connected. Characters: {all_characters}, Connected: {connected_characters}")
    
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")
            
            if msg_type == "start_conversation" and orchestrator:
                npc1 = data.get("npc1_id", "").strip()
                npc2 = data.get("npc2_id", "").strip()  # Can be empty for monologue
                turns = int(data.get("turns", 2))
                context = data.get("context", "").strip()
                
                # Trigger info (optional, for auto-triggered conversations)
                trigger_id = data.get("trigger_id")
                trigger_category = data.get("trigger_category")
                
                if not npc1:
                    await ws.send_json({"type": "error", "message": "NPC 1 is required"})
                    continue
                
                config = NPCConversationConfig(
                    npc1_id=npc1,
                    npc2_id=npc2 if npc2 else None,
                    total_turns=max(1, min(turns, 20)),  # Limit 1-20 turns
                    context=context,
                    trigger_id=trigger_id,
                    trigger_category=trigger_category
                )
                
                success = await orchestrator.start_conversation(config)
                if not success:
                    await ws.send_json({
                        "type": "error", 
                        "message": orchestrator.state.error or "Failed to start conversation"
                    })
            
            elif msg_type == "stop_conversation" and orchestrator:
                orchestrator.stop_conversation()
                
            elif msg_type == "reset" and orchestrator:
                orchestrator.reset()
                await ws.send_json({"type": "npc_conversation_state", **orchestrator.get_state().to_dict()})
                
    except Exception as e:
        logger.info(f"🎭🔌 NPC Conversation client disconnected: {e}")
    finally:
        if ws in app.state.NPCConversationClients:
            app.state.NPCConversationClients.remove(ws)


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

                # If using shared STT, dedupe identical mic frames across characters
                if getattr(app.state, "SharedSttEnabled", False) and session.audio_queue is getattr(app.state, "SharedAudioQueue", None):
                    key = (timestamp_ms, len(raw))
                    seen_set = getattr(app.state, "SharedAudioSeenSet", None)
                    seen_queue = getattr(app.state, "SharedAudioSeen", None)
                    max_seen = int(getattr(app.state, "SharedAudioSeenMax", 200))
                    if seen_set is not None and seen_queue is not None:
                        if key in seen_set:
                            continue  # Duplicate frame from another character, skip
                        seen_set.add(key)
                        seen_queue.append(key)
                        if len(seen_queue) > max_seen:
                            old = seen_queue.popleft()
                            seen_set.discard(old)
                    
                    # Track this character as the active conversation target
                    # (the one the player is currently talking to - first to send non-duplicate audio)
                    prev_target = getattr(app.state, "ActiveConversationTarget", None)
                    if prev_target != session.character_id:
                        logger.info(f"🎯 Active conversation target: {session.character_id} (was: {prev_target})")
                    app.state.ActiveConversationTarget = session.character_id
                    app.state.ActiveConversationTargetTime = time.time()

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
                        
                        # Log direct injection
                        conv_logger = get_conversation_logger()
                        if conv_logger:
                            conv_logger.log_injection(session.character_id, content, source="direct")
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

async def _reset_interrupt_flag_async(callbacks: 'TranscriptionCallbacks'):
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
    # Check the connection-specific interruption state
    if callbacks.mic_interrupted:
        logger.info(f"{Colors.apply('🖥️🎙️ ▶️ Microphone continued (async reset)').cyan}")
        callbacks.mic_interrupted = False
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
                session.callbacks
                and session.callbacks.mic_interrupted
                and session.callbacks.interruption_time
                and time.time() - session.callbacks.interruption_time > 2.0
            ):
                session.callbacks.mic_interrupted = False
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
                    int(session.callbacks.mic_interrupted if session.callbacks else 0) # Input processor state
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
            if session.callbacks and not session.callbacks.tts_chunk_sent:
                # Use the async helper function instead of a thread
                asyncio.create_task(_reset_interrupt_flag_async(session.callbacks))

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
    def __init__(self, session: CharacterSession, stop_npc_conversation: Optional[Callable] = None):
        """
        Initializes the TranscriptionCallbacks instance for a WebSocket connection.

        Args:
            session: The character session this callback belongs to.
            stop_npc_conversation: Optional callback to stop NPC-to-NPC conversations when player speaks.
        """
        self.session = session
        self.stop_npc_conversation = stop_npc_conversation
        # Fallback gating: some setups won't fire on_recording_start reliably for every utterance.
        # We'll also trigger NPC-conv interruption once per "speech segment" when we see first partial text.
        self._npc_conv_interrupt_sent = False
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
        self.mic_interrupted: bool = False

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
        
        # Timing tracking for logging
        self.recording_start_time: float = 0.0  # When user started speaking
        self.llm_start_time: float = 0.0  # When LLM generation was triggered (may be before turn end due to speculative gen)
        self.turn_end_time: float = 0.0  # When user finished speaking (THE reference point for perceived latency)
        self.first_audio_time: float = 0.0  # When first audio chunk was synthesized
        self.ttfa_ms: float = 0.0  # Time To First Audio: turn_end → first_audio (THE key metric)

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
        self.mic_interrupted = False

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
        
        # Reset timing variables (but keep recording_start_time as it's set per-utterance)
        # Note: llm_start_time persists through the turn, only reset after response sent
        self.turn_end_time = 0.0
        self.first_audio_time = 0.0
        self.ttfa_ms = 0.0

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
        # Fallback: if recording_start didn't fire (or was missed), first partial text should still interrupt NPC-to-NPC.
        if (not self._npc_conv_interrupt_sent) and self.stop_npc_conversation and txt and txt.strip():
            try:
                self.stop_npc_conversation()
                self._npc_conv_interrupt_sent = True
            except Exception as e:
                logger.warning(f"🖥️⚠️ Failed to stop NPC conversation (on_partial fallback): {e}")
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
        # With shared STT, only generate for the active conversation target
        # This prevents both NPCs from trying to respond when player is in range of multiple
        if self.session.uses_shared_audio:
            active_target = getattr(app.state, "ActiveConversationTarget", None)
            if active_target and active_target != self.session.character_id:
                logger.debug(f"🖥️🧠 Skipping generation for {self.session.character_id} (active target: {active_target})")
                return
        
        # Track LLM start time for logging
        self.llm_start_time = time.time()
        
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
        # With shared STT, only process final for the active conversation target
        if self.session.uses_shared_audio:
            active_target = getattr(app.state, "ActiveConversationTarget", None)
            if active_target and active_target != self.session.character_id:
                logger.debug(f"🖥️🏁 Skipping on_before_final for {self.session.character_id} (active target: {active_target})")
                return
        
        # Record turn end time - THIS is when perceived latency measurement should start
        self.turn_end_time = time.time()
        logger.info(Colors.apply('🖥️🏁 =================== USER TURN END ===================').light_gray)
        self.user_finished_turn = True
        self.user_interrupted = False # Reset connection-specific flag (user finished, not interrupted)
        # Access global manager state
        if self.session.pipeline and self.session.pipeline.is_valid_gen():
            logger.info(f"{Colors.apply('🖥️🔊 TTS ALLOWED (before final)').blue}")
            self.session.pipeline.running_generation.tts_quick_allowed_event.set()

        # first block further incoming audio (connection-specific state)
        if not self.mic_interrupted:
            logger.info(f"{Colors.apply('🖥️🎙️ ⏸️ Microphone interrupted (end of turn)').cyan}")
            self.mic_interrupted = True
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
        
        # Log player utterance with timing
        conv_logger = get_conversation_logger()
        if conv_logger:
            transcription_time_ms = None
            if self.recording_start_time > 0:
                transcription_time_ms = (time.time() - self.recording_start_time) * 1000
            conv_logger.log_player_utterance(
                character_id=self.session.character_id,
                text=user_request_content,
                is_partial=False,
                transcription_time_ms=transcription_time_ms
            )

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
        # Reset fallback gate once we return to silence.
        if silence_active:
            self._npc_conv_interrupt_sent = False

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

    def on_first_audio_chunk(self, first_audio_timestamp: float):
        """
        Callback invoked when the first TTS audio chunk is synthesized.
        
        Logs the Time To First Audio (TTFA) - crucial for latency perception.
        
        Args:
            first_audio_timestamp: The time.time() when first audio was ready.
        """
        self.first_audio_time = first_audio_timestamp
        
        # TTFA: Time from user turn end to first audio ready
        # This is what the user PERCEIVES as response latency
        if self.turn_end_time > 0:
            self.ttfa_ms = (first_audio_timestamp - self.turn_end_time) * 1000
            logger.info(f"🖥️🎵 TTFA: {self.ttfa_ms:.1f}ms (user stop → first audio)")
        elif self.llm_start_time > 0:
            # Fallback if turn_end not set (shouldn't happen normally)
            self.ttfa_ms = (first_audio_timestamp - self.llm_start_time) * 1000
            logger.info(f"🖥️🎵 TTFA (fallback): {self.ttfa_ms:.1f}ms")

    def on_recording_start(self):
        """
        Callback invoked when the audio input processor starts recording user speech.

        If client-side TTS is playing, it triggers an interruption: stops server-side
        TTS streaming, sends stop/interruption messages to the client, aborts ongoing
        generation, sends any final assistant answer generated so far, and resets relevant state.
        
        Also stops any ongoing NPC-to-NPC conversations since the player is now speaking.
        """
        # Track recording start time for logging
        self.recording_start_time = time.time()
        
        logger.info(f"{Colors.ORANGE}🖥️🎙️ Recording started.{Colors.RESET} TTS Client Playing: {self.tts_client_playing}")
        
        # Stop NPC conversation if one is running - player is now speaking
        if self.stop_npc_conversation:
            try:
                self.stop_npc_conversation()
                self._npc_conv_interrupt_sent = True
            except Exception as e:
                logger.warning(f"🖥️⚠️ Failed to stop NPC conversation: {e}")
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
                    # Mark first-contact introduction as done (3-layer prompt feature)
                    if hasattr(self.session.pipeline, "mark_introduced_to_player"):
                        try:
                            self.session.pipeline.mark_introduced_to_player()
                        except Exception:
                            pass
                self.final_assistant_answer_sent = True
                self.final_assistant_answer = cleaned_answer # Store the sent answer
                
                # Log NPC response with timing
                conv_logger = get_conversation_logger()
                if conv_logger:
                    now = time.time()
                    
                    # Total response time: from user turn end to now
                    total_time_ms = None
                    if self.turn_end_time > 0:
                        total_time_ms = (now - self.turn_end_time) * 1000
                    
                    conv_logger.log_npc_response(
                        character_id=self.session.character_id,
                        text=cleaned_answer,
                        was_interrupted=self.user_interrupted,
                        ttfa_ms=self.ttfa_ms if self.ttfa_ms > 0 else None,
                        total_time_ms=total_time_ms
                    )
                    # Reset timing for next response
                    self.ttfa_ms = 0.0
                    self.first_audio_time = 0.0
                    self.turn_end_time = 0.0
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
    
    # Debug: Log pre-init status
    logger.info(f"🔍 Looking for character_id='{character_id}' in CharacterSessions (keys: {list(app.state.CharacterSessions.keys())})")
    if existing_session:
        logger.info(f"🔍 Found existing session for '{character_id}': pipeline={'YES' if existing_session.pipeline else 'NO'}, message_queue={'YES' if existing_session.message_queue else 'NO'}")
    else:
        logger.info(f"🔍 No existing session for '{character_id}' - will create new one")
    
    if existing_session and existing_session.message_queue is not None:
        logger.warning(f"🖥️⚠️ Character {character_id} already has an active connection; dropping new attempt")
        await ws.close(code=4409)
        return

    config = app.state.CharacterConfig.get(character_id, {})
    session = existing_session or CharacterSession(character_id=character_id, config=config)
    app.state.CharacterSessions[character_id] = session
    
    
    shared_stt = getattr(app.state, "SharedSttEnabled", False)
    if shared_stt:
        session.audio_input = getattr(app.state, "SharedAudioInput", None)
        session.audio_queue = getattr(app.state, "SharedAudioQueue", None)
        session.uses_shared_audio = True
        logger.info("🎙️ Using shared STT pipeline for audio input")
    else:
        if audio_input_needs_recreate(session.audio_input):
            if session.audio_input is not None:
                logger.warning(f"🔧 Recreating AudioInputProcessor for {character_id} (previous one was not healthy)...")
                try:
                    session.audio_input.shutdown()
                except Exception as e:
                    logger.warning(f"🔧⚠️ Failed to shutdown unhealthy AudioInputProcessor for {character_id}: {e}")

            logger.info(f"🔧 Creating new AudioInputProcessor for {character_id} (NOT pre-initialized)...")
            audio_start = time.time()
            session.audio_input = AudioInputProcessor(
                LANGUAGE,
                is_orpheus=(session.config.get("tts_engine", DEFAULT_ENGINE) == "orpheus"),
                pipeline_latency=0.5,
            )
            logger.info(f"🔧 AudioInputProcessor created in {time.time() - audio_start:.2f}s")
        else:
            logger.info(f"🚀✅ Using PRE-INITIALIZED AudioInputProcessor for {character_id}")

    if session.pipeline is None:
        logger.info(f"🔧 Creating new SpeechPipelineManager for {character_id} (NOT pre-initialized)...")
        pipeline_start = time.time()
        # 3-Layer Prompt System: personality (Layer 2) + game_knowledge (Layer 3)
        # Layer 1 (Framework) is built-in to prompt_layers.py
        personality = session.config.get("personality")
        game_knowledge = combine_game_knowledge(
            getattr(app.state, "StoryBible", ""),
            session.config.get("game_knowledge"),
        )
        # Legacy support: use system_prompt if no 3-layer fields
        system_prompt = session.config.get("system_prompt") if not personality and not game_knowledge else None
        
        history = session.config.get("history", [])
        session.pipeline = SpeechPipelineManager(
            tts_engine=session.config.get("tts_engine", DEFAULT_ENGINE),
            llm_provider=session.config.get("llm_provider", DEFAULT_LLM_PROVIDER),
            llm_model=session.config.get("llm_model", DEFAULT_LLM_MODEL),
            no_think=session.config.get("no_think", DEFAULT_NO_THINK),
            orpheus_model=session.config.get("orpheus_model", DEFAULT_ORPHEUS_MODEL),
            personality=personality,
            game_knowledge=game_knowledge,
            system_prompt_override=system_prompt,
            history=history,
            voice=session.config.get("voice"),
            reference_audio=session.config.get("reference_audio"),
            session_id=character_id,
        )
        logger.info(f"🔧 SpeechPipelineManager created in {time.time() - pipeline_start:.2f}s")
    else:
        logger.info(f"🚀✅ Using PRE-INITIALIZED pipeline for {character_id} (skipped {10}-20s load time)")
    
    # Set up message queue
    message_queue = session.message_queue or asyncio.Queue()
    session.message_queue = message_queue

    audio_chunks = session.audio_queue or asyncio.Queue()
    session.audio_queue = audio_chunks

    # Create stop function for NPC conversations
    def stop_npc_conv():
        npc_conv = getattr(app.state, 'NPCConversation', None)
        loop = getattr(app.state, "main_loop", None)
        broadcast_interrupt = getattr(app.state, "broadcast_npc_conversation_interrupted", None)

        def _do_on_loop():
            # Always broadcast "stop playback" to NPC-conversation clients.
            # Even if the orchestrator already stopped (or isn't in RUNNING), clients may still be playing buffered audio.
            try:
                if broadcast_interrupt:
                    asyncio.create_task(broadcast_interrupt(reason="player_speaking"))
            except Exception as e:
                logger.warning(f"🎭⚠️ Failed to schedule npc_conversation_interrupted broadcast: {e}")

            if npc_conv and hasattr(npc_conv, 'state'):
                try:
                    from npc_conversation import ConversationState
                    if npc_conv.state.state == ConversationState.RUNNING:
                        logger.info("🖥️🛑 Stopping NPC conversation - player is speaking")
                        npc_conv.stop_conversation(reason="player_speaking")
                except Exception as e:
                    logger.warning(f"🖥️⚠️ Failed to stop NPC conversation (stop_npc_conv): {e}")

        # This callback can run from a background thread (audio pipeline).
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_do_on_loop)
        else:
            # Best-effort fallback
            _do_on_loop()
    
    callbacks = TranscriptionCallbacks(session, stop_npc_conversation=stop_npc_conv)

    # Wire callbacks to the shared/session audio processor
    if session.audio_input:
        session.audio_input.add_listener(callbacks)

    session.pipeline.on_partial_assistant_text = callbacks.on_partial_assistant_text
    session.pipeline.on_first_audio_callback = callbacks.on_first_audio_chunk

    tasks = [
        asyncio.create_task(process_incoming_data(ws, session)),
        asyncio.create_task(send_text_messages(ws, session)),
        asyncio.create_task(send_tts_chunks(app, session)),
    ]
    if not shared_stt:
        tasks.insert(1, asyncio.create_task(session.audio_input.process_chunk_queue(audio_chunks)))
    session.tasks = tasks
    
    # Send character_ready signal - initialization is complete
    logger.info(f"🖥️✅ Character {character_id} fully initialized and ready")
    await ws.send_json({
        "type": "character_ready",
        "character_id": character_id
    })
    
    # Log character connection
    conv_logger = get_conversation_logger()
    if conv_logger:
        conv_logger.log_character_connect(character_id)
    
    # Notify NPC conversation clients about updated character list
    if hasattr(app.state, 'broadcast_npc_character_update'):
        await app.state.broadcast_npc_character_update()

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
        
        # Log character disconnection
        conv_logger = get_conversation_logger()
        if conv_logger:
            conv_logger.log_character_disconnect(character_id)

        if session.audio_input and session.callbacks:
            session.audio_input.remove_listener(session.callbacks)

        session.stop_connection()
        if session.config.get("persist_history", False) and session.pipeline:
            session.config["history"] = session.pipeline.history
        else:
            session.pipeline.history.clear() if session.pipeline else None

# --------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------
if __name__ == "__main__":
    # Get server config for host/port
    _startup_config = get_server_config()
    host = _startup_config.server.host
    port = _startup_config.server.port

    # Run the server without SSL
    if not USE_SSL:
        logger.info(f"🖥️▶️ Starting server without SSL on {host}:{port}")
        # Pass app object directly (not string) to avoid double-import issues
        # This ensures pre-initialized state persists
        uvicorn.run(app, host=host, port=port, log_config=None)

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
        logger.info(f"🖥️▶️ Starting server with SSL on {host}:{port} (cert: {cert_file}, key: {key_file}).")
        # Pass app object directly (not string) to avoid double-import issues
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_config=None,
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
