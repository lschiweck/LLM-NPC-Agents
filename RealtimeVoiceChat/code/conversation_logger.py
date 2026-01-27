# conversation_logger.py
"""
Conversation logging system for tracking player-NPC and NPC-NPC interactions.

Logs are stored as JSONL (JSON Lines) files for easy parsing and analysis.
Each session gets its own log file with timestamped entries.

CRASH-SAFE: Uses immediate flush and atexit handler to ensure logs persist.
"""

import atexit
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """A single log entry."""
    timestamp: str
    event_type: str
    character_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "character_id": self.character_id,
            **self.data
        }


class ConversationLogger:
    """
    Handles logging of conversations, processing times, and game events.
    
    All logging is controlled by the server_config.json settings.
    When disabled, all methods become no-ops for zero overhead.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the conversation logger.
        
        Args:
            config: The 'logging' section from server_config.json
        """
        self.enabled = config.get("enabled", False)
        self.output_dir = Path(config.get("output_dir", "./conversation_logs"))
        
        # Individual log toggles
        self.log_player_utterances = config.get("log_player_utterances", True)
        self.log_npc_responses = config.get("log_npc_responses", True)
        self.log_processing_times = config.get("log_processing_times", True)
        self.log_injections = config.get("log_injections", True)
        self.log_npc_conversations = config.get("log_npc_conversations", True)
        self.log_director_notes = config.get("log_director_notes", True)

        # Flush policy (reduce blocking I/O on hot paths)
        self.flush_every_n = int(config.get("flush_every_n", 20))
        self.flush_interval_seconds = float(config.get("flush_interval_seconds", 1.0))
        self._pending_flush = 0
        self._last_flush_time = time.time()
        
        # Session tracking
        self.session_id: Optional[str] = None
        self.session_file: Optional[Path] = None
        self._file_handle = None
        
        if self.enabled:
            self._setup_logging_dir()
            self._start_session()
            self._register_exit_handlers()
    
    def _setup_logging_dir(self):
        """Create the logging directory if it doesn't exist."""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"📝 Conversation logging enabled. Output dir: {self.output_dir}")
        except Exception as e:
            logger.error(f"📝💥 Failed to create log directory: {e}")
            self.enabled = False
    
    def _register_exit_handlers(self):
        """Register handlers to ensure logs are saved on exit/crash."""
        # atexit handles normal exits
        atexit.register(self._on_exit)
        
        # Handle SIGTERM (kill) and SIGINT (Ctrl+C)
        # Store original handlers to chain them
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        self._original_sigint = signal.getsignal(signal.SIGINT)
        
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        logger.info("📝 Registered exit handlers for crash-safe logging")
    
    def _signal_handler(self, signum, frame):
        """Handle termination signals to ensure logs are saved."""
        self._on_exit(reason=f"signal_{signum}")
        
        # Call original handler
        if signum == signal.SIGTERM and callable(self._original_sigterm):
            self._original_sigterm(signum, frame)
        elif signum == signal.SIGINT and callable(self._original_sigint):
            self._original_sigint(signum, frame)
        
        # Re-raise to allow normal shutdown
        sys.exit(128 + signum)
    
    def _on_exit(self, reason: str = "normal"):
        """Called on exit to finalize logs."""
        if not self.enabled or not self._file_handle:
            return
            
        try:
            self._write_entry(LogEntry(
                timestamp=self._get_timestamp(),
                event_type="session_end",
                data={"session_id": self.session_id, "reason": reason}
            ))
            self._file_handle.close()
            self._file_handle = None
            logger.info(f"📝 Session closed ({reason}): {self.session_file}")
        except Exception as e:
            # Can't do much if this fails during exit
            pass
    
    def _start_session(self):
        """Start a new logging session with a timestamped file."""
        if not self.enabled:
            return
            
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_file = self.output_dir / f"session_{self.session_id}.jsonl"
        
        try:
            self._file_handle = open(self.session_file, "a", encoding="utf-8")
            self._write_entry(LogEntry(
                timestamp=self._get_timestamp(),
                event_type="session_start",
                data={"session_id": self.session_id}
            ))
            self._last_flush_time = time.time()
            logger.info(f"📝 Started logging session: {self.session_file}")
        except Exception as e:
            logger.error(f"📝💥 Failed to start logging session: {e}")
            self.enabled = False
    
    def _get_timestamp(self) -> str:
        """Get current timestamp in ISO format."""
        return datetime.now().isoformat()
    
    def _write_entry(self, entry: LogEntry):
        """Write a log entry to the session file."""
        if not self.enabled or not self._file_handle:
            return
            
        try:
            self._file_handle.write(json.dumps(entry.to_dict()) + "\n")
            self._pending_flush += 1
            now = time.time()
            if (
                self._pending_flush >= self.flush_every_n
                or (now - self._last_flush_time) >= self.flush_interval_seconds
            ):
                self._file_handle.flush()
                self._pending_flush = 0
                self._last_flush_time = now
        except Exception as e:
            logger.warning(f"📝⚠️ Failed to write log entry: {e}")
    
    # =========================================================================
    # Public logging methods
    # =========================================================================
    
    def log_player_utterance(
        self,
        character_id: str,
        text: str,
        is_partial: bool = False,
        transcription_time_ms: Optional[float] = None
    ):
        """Log a player's speech (transcription)."""
        if not self.enabled or not self.log_player_utterances:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="player_utterance",
            character_id=character_id,
            data={
                "text": text,
                "is_partial": is_partial,
                "transcription_time_ms": transcription_time_ms
            }
        ))
    
    def log_npc_response(
        self,
        character_id: str,
        text: str,
        ttfa_ms: Optional[float] = None,
        total_time_ms: Optional[float] = None,
        was_interrupted: bool = False
    ):
        """Log an NPC's response to a player.
        
        Args:
            character_id: The character ID
            text: The response text
            ttfa_ms: Time To First Audio in ms (from user turn end to first audio chunk ready)
                     This is THE key latency metric - what the user perceives as response time.
            total_time_ms: Total response time in ms (from user turn end to response complete)
            was_interrupted: Whether the response was interrupted by the player
        """
        if not self.enabled or not self.log_npc_responses:
            return
            
        data = {
            "text": text,
            "was_interrupted": was_interrupted
        }
        
        if self.log_processing_times:
            data["ttfa_ms"] = ttfa_ms
            data["total_time_ms"] = total_time_ms
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="npc_response",
            character_id=character_id,
            data=data
        ))
    
    def log_injection(
        self,
        character_id: str,
        content: str,
        source: str = "direct"  # "direct" or "game_manager"
    ):
        """Log an injection (director's note)."""
        if not self.enabled or not self.log_injections:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="injection",
            character_id=character_id,
            data={
                "content": content,
                "source": source
            }
        ))
    
    def log_director_notes(
        self,
        character_id: str,
        notes: List[str]
    ):
        """Log the current director notes for a character."""
        if not self.enabled or not self.log_director_notes:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="director_notes_state",
            character_id=character_id,
            data={"notes": notes}
        ))
    
    def log_npc_conversation_turn(
        self,
        speaker_id: str,
        listener_id: Optional[str],
        message: str,
        turn_number: int,
        context: Optional[str] = None,
        llm_time_ms: Optional[float] = None,
        tts_time_ms: Optional[float] = None
    ):
        """Log a turn in an NPC-to-NPC conversation."""
        if not self.enabled or not self.log_npc_conversations:
            return
            
        data = {
            "message": message,
            "listener_id": listener_id,
            "turn_number": turn_number,
            "context": context
        }
        
        if self.log_processing_times:
            data["llm_time_ms"] = llm_time_ms
            data["tts_time_ms"] = tts_time_ms
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="npc_conversation_turn",
            character_id=speaker_id,
            data=data
        ))
    
    def log_npc_conversation_start(
        self,
        participants: List[str],
        context: Optional[str] = None,
        total_turns: int = 0,
        trigger_id: Optional[str] = None,
        trigger_category: Optional[str] = None
    ):
        """Log the start of an NPC-to-NPC conversation.
        
        Args:
            participants: List of NPC IDs in the conversation
            context: The conversation prompt/context
            total_turns: Number of turns planned
            trigger_id: Which trigger started this conversation (if auto-triggered)
            trigger_category: Category of trigger ("location", "fallback", or None if manual)
        """
        if not self.enabled or not self.log_npc_conversations:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="npc_conversation_start",
            data={
                "participants": participants,
                "context": context,
                "total_turns": total_turns,
                "trigger_id": trigger_id,
                "trigger_category": trigger_category
            }
        ))
    
    def log_npc_conversation_end(
        self,
        participants: List[str],
        turns_completed: int,
        was_stopped: bool = False
    ):
        """Log the end of an NPC-to-NPC conversation."""
        if not self.enabled or not self.log_npc_conversations:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="npc_conversation_end",
            data={
                "participants": participants,
                "turns_completed": turns_completed,
                "was_stopped": was_stopped
            }
        ))
    
    def log_game_manager_tick(
        self,
        thinking: str,
        actions: List[Dict[str, str]],
        processing_time_ms: Optional[float] = None
    ):
        """Log a Game Manager tick."""
        if not self.enabled or not self.log_injections:
            return
            
        data = {
            "thinking": thinking,
            "actions": actions
        }
        
        if self.log_processing_times:
            data["processing_time_ms"] = processing_time_ms
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="game_manager_tick",
            data=data
        ))
    
    def log_character_connect(self, character_id: str):
        """Log when a character connects."""
        if not self.enabled:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="character_connect",
            character_id=character_id
        ))
    
    def log_character_disconnect(self, character_id: str):
        """Log when a character disconnects."""
        if not self.enabled:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type="character_disconnect",
            character_id=character_id
        ))
    
    def log_custom_event(
        self,
        event_type: str,
        character_id: Optional[str] = None,
        **kwargs
    ):
        """Log a custom event with arbitrary data."""
        if not self.enabled:
            return
            
        self._write_entry(LogEntry(
            timestamp=self._get_timestamp(),
            event_type=event_type,
            character_id=character_id,
            data=kwargs
        ))
    
    def close(self):
        """Close the logging session (calls _on_exit internally)."""
        self._on_exit(reason="explicit_close")


# Global logger instance (set during server startup)
_conversation_logger: Optional[ConversationLogger] = None


def init_conversation_logger(config: Dict[str, Any]) -> ConversationLogger:
    """Initialize the global conversation logger."""
    global _conversation_logger
    _conversation_logger = ConversationLogger(config)
    return _conversation_logger


def get_conversation_logger() -> Optional[ConversationLogger]:
    """Get the global conversation logger instance."""
    return _conversation_logger
