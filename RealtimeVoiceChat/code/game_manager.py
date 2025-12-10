# game_manager.py
"""
Game Manager - Background LLM that orchestrates NPC behavior based on story context.

Runs on a configurable tick interval, analyzes all NPC conversations,
and injects instructions to characters to keep the story engaging.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field

from llm_module import LLM

logger = logging.getLogger(__name__)


@dataclass
class GameManagerState:
    """Tracks the current state of the Game Manager."""
    enabled: bool = False
    last_tick_time: float = 0
    next_tick_time: float = 0
    last_thinking: str = ""
    last_actions: List[Dict[str, str]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)  # Log of all decisions
    is_processing: bool = False
    clues: List[str] = field(default_factory=list)  # Game clues injected by the game


class GameManager:
    """
    Background LLM controller that manages story progression.
    
    Periodically analyzes NPC conversations and injects instructions
    to keep the narrative engaging and coherent.
    """
    
    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize the Game Manager.
        
        Args:
            config_path: Path to game_manager_config.json. 
                        Defaults to same directory as this file.
        """
        if config_path is None:
            config_path = Path(__file__).resolve().parent / "game_manager_config.json"
        
        self.config_path = config_path
        self.config = self._load_config()
        self.state = GameManagerState(enabled=self.config.get("enabled", False))
        
        self.llm: Optional[LLM] = None
        self._task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Callback to inject into characters - set by server.py
        self.on_inject: Optional[Callable[[str, str], None]] = None
        
        # Callback to get character histories - set by server.py
        self.get_character_histories: Optional[Callable[[], Dict[str, List[Dict]]]] = None
        
        # Callback to broadcast state updates to UI
        self.on_state_update: Optional[Callable[[Dict], None]] = None
        
        if self.state.enabled:
            self._init_llm()
            logger.info("🎮 Game Manager initialized (enabled)")
        else:
            logger.info("🎮 Game Manager initialized (disabled)")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        if not self.config_path.exists():
            logger.warning(f"🎮⚠️ Config not found at {self.config_path}, using defaults")
            return {
                "enabled": False,
                "tick_interval_seconds": 30,
                "llm_provider": "ollama",
                "llm_model": "llama3",
                "story_context": "",
                "known_characters": [],
                "system_prompt": "You are a game master."
            }
        
        try:
            with self.config_path.open("r", encoding="utf-8") as f:
                config = json.load(f)
            logger.info(f"🎮📄 Loaded config from {self.config_path}")
            return config
        except Exception as e:
            logger.error(f"🎮💥 Failed to load config: {e}")
            return {"enabled": False}
    
    def _init_llm(self):
        """Initialize the LLM for the game manager."""
        try:
            self.llm = LLM(
                backend=self.config.get("llm_provider", "ollama"),
                model=self.config.get("llm_model", "llama3"),
                system_prompt=self.config.get("system_prompt", "You are a game master."),
            )
            logger.info(f"🎮🧠 Game Manager LLM initialized: {self.config.get('llm_model')}")
        except Exception as e:
            logger.error(f"🎮💥 Failed to initialize LLM: {e}")
            self.state.enabled = False
    
    def reload_config(self):
        """Reload configuration from file."""
        self.config = self._load_config()
        was_enabled = self.state.enabled
        self.state.enabled = self.config.get("enabled", False)
        
        if self.state.enabled and not was_enabled:
            self._init_llm()
        
        logger.info(f"🎮🔄 Config reloaded. Enabled: {self.state.enabled}")
    
    def get_state_for_ui(self) -> Dict[str, Any]:
        """Get current state formatted for the UI."""
        now = time.time()
        seconds_until_tick = max(0, self.state.next_tick_time - now)
        
        return {
            "enabled": self.state.enabled,
            "is_processing": self.state.is_processing,
            "seconds_until_tick": int(seconds_until_tick),
            "tick_interval": self.config.get("tick_interval_seconds", 30),
            "last_thinking": self.state.last_thinking,
            "last_actions": self.state.last_actions,
            "history": self.state.history[-10:],  # Last 10 entries
            "clues": list(self.state.clues),  # Current game clues
        }
    
    def inject_clue(self, clue: str):
        """Add a game clue that will be included in the next tick's prompt."""
        self.state.clues.append(clue)
        logger.info(f"🎮💡 Game clue added: {clue}")
        self._broadcast_state()
    
    def remove_clue(self, index: int):
        """Remove a clue by index."""
        if 0 <= index < len(self.state.clues):
            removed = self.state.clues.pop(index)
            logger.info(f"🎮💡 Game clue removed: {removed}")
            self._broadcast_state()
    
    def clear_clues(self):
        """Clear all clues."""
        self.state.clues.clear()
        logger.info("🎮💡 All game clues cleared")
        self._broadcast_state()
    
    def _build_prompt(self, character_histories: Dict[str, List[Dict]]) -> str:
        """Build the prompt for the Game Manager LLM."""
        story_context = self.config.get("story_context", "")
        known_characters = self.config.get("known_characters", [])
        
        prompt_parts = [
            "=== STORY KONTEXT ===",
            story_context,
            "",
            "=== BEKANNTE CHARAKTERE ===",
            ", ".join(known_characters),
            "",
        ]
        
        # Add game clues if any
        if self.state.clues:
            prompt_parts.extend([
                "=== SPIELHINWEISE (vom Spiel gemeldet) ===",
                *[f"• {clue}" for clue in self.state.clues],
                "",
            ])
        
        prompt_parts.append("=== GESPRÄCHE MIT DEM SPIELER ===")
        
        for char_id, history in character_histories.items():
            prompt_parts.append(f"\n--- {char_id} ---")
            if not history:
                prompt_parts.append("(Noch kein Gespräch)")
            else:
                for msg in history[-20:]:  # Last 20 messages per character
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                    if role == "user":
                        prompt_parts.append(f"SPIELER: {content}")
                    elif role == "assistant":
                        prompt_parts.append(f"{char_id}: {content}")
        
        prompt_parts.extend([
            "",
            "=== DEINE AUFGABE ===",
            "Analysiere die Gespräche und die Spielhinweise.",
            "Entscheide ob ein Charakter neue Instruktionen braucht basierend auf den Hinweisen.",
            "Halte die Story spannend und kohärent.",
        ])
        
        return "\n".join(prompt_parts)
    
    def _parse_response(self, response: str) -> tuple[str, List[Dict[str, str]]]:
        """
        Parse the LLM response to extract thinking and actions.
        
        Returns:
            Tuple of (thinking_text, list_of_actions)
            Each action is {"target": "CharacterID", "instruction": "..."}
        """
        thinking = ""
        actions = []
        
        # Extract THINKING
        thinking_match = re.search(r"THINKING:\s*(.+?)(?=ACTION:|$)", response, re.DOTALL | re.IGNORECASE)
        if thinking_match:
            thinking = thinking_match.group(1).strip()
        
        # Extract INJECT actions
        inject_pattern = r"INJECT\s+(\w+):\s*(.+?)(?=INJECT|ACTION:|$)"
        for match in re.finditer(inject_pattern, response, re.DOTALL | re.IGNORECASE):
            target = match.group(1).strip()
            instruction = match.group(2).strip()
            if instruction and target:
                actions.append({"target": target, "instruction": instruction})
        
        # Check for NONE action
        if re.search(r"ACTION:\s*NONE", response, re.IGNORECASE):
            actions = []  # Explicitly no actions
        
        return thinking, actions
    
    async def _tick(self):
        """Execute a single tick of the Game Manager."""
        if not self.state.enabled or self.llm is None:
            return
        
        if self.get_character_histories is None:
            logger.warning("🎮⚠️ No character history callback set, skipping tick")
            return
        
        self.state.is_processing = True
        self._broadcast_state()
        
        try:
            # Get all character histories
            character_histories = self.get_character_histories()
            
            # Build prompt
            prompt = self._build_prompt(character_histories)
            
            logger.info("🎮🧠 Game Manager thinking...")
            
            # Generate response (no semaphore - runs in parallel)
            full_response = ""
            try:
                for chunk in self.llm.generate(text=prompt, history=None, use_system_prompt=True):
                    full_response += chunk
            except Exception as e:
                logger.error(f"🎮💥 LLM generation failed: {e}")
                self.state.is_processing = False
                self._broadcast_state()
                return
            
            logger.info(f"🎮📝 Game Manager response:\n{full_response}")
            
            # Parse response
            thinking, actions = self._parse_response(full_response)
            
            self.state.last_thinking = thinking
            self.state.last_actions = actions
            
            # Execute injections
            for action in actions:
                target = action["target"]
                instruction = action["instruction"]
                
                if self.on_inject:
                    logger.info(f"🎮💉 Injecting into {target}: {instruction[:50]}...")
                    self.on_inject(target, instruction)
                else:
                    logger.warning(f"🎮⚠️ No inject callback set, cannot inject into {target}")
            
            # Log to history
            self.state.history.append({
                "timestamp": time.time(),
                "thinking": thinking,
                "actions": actions,
            })
            
            # Keep history bounded
            if len(self.state.history) > 100:
                self.state.history = self.state.history[-100:]
            
            if actions:
                logger.info(f"🎮✅ Tick complete: {len(actions)} injection(s)")
            else:
                logger.info("🎮✅ Tick complete: no changes")
                
        except Exception as e:
            logger.exception(f"🎮💥 Tick failed: {e}")
        finally:
            self.state.is_processing = False
            self._broadcast_state()
    
    def _broadcast_state(self):
        """Broadcast current state to UI."""
        if self.on_state_update:
            try:
                self.on_state_update(self.get_state_for_ui())
            except Exception as e:
                logger.warning(f"🎮⚠️ Failed to broadcast state: {e}")
    
    async def _run_loop(self):
        """Main loop that runs ticks at the configured interval."""
        interval = self.config.get("tick_interval_seconds", 30)
        
        logger.info(f"🎮▶️ Game Manager loop started (interval: {interval}s)")
        
        while not self._shutdown_event.is_set():
            if not self.state.enabled:
                await asyncio.sleep(1)
                continue
            
            # Update next tick time
            self.state.next_tick_time = time.time() + interval
            self._broadcast_state()
            
            # Wait for interval (with periodic state broadcasts for countdown)
            for _ in range(interval):
                if self._shutdown_event.is_set():
                    break
                await asyncio.sleep(1)
                self._broadcast_state()
            
            if self._shutdown_event.is_set():
                break
            
            # Execute tick
            self.state.last_tick_time = time.time()
            await self._tick()
        
        logger.info("🎮⏹️ Game Manager loop stopped")
    
    def start(self):
        """Start the Game Manager background loop."""
        if self._task is not None and not self._task.done():
            logger.warning("🎮⚠️ Game Manager already running")
            return
        
        self._shutdown_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("🎮🚀 Game Manager started")
    
    def stop(self):
        """Stop the Game Manager background loop."""
        self._shutdown_event.set()
        if self._task is not None:
            self._task.cancel()
        logger.info("🎮⏹️ Game Manager stop requested")
    
    def trigger_tick_now(self):
        """Manually trigger a tick immediately."""
        if not self.state.enabled:
            logger.warning("🎮⚠️ Cannot trigger tick: Game Manager is disabled")
            return
        
        asyncio.create_task(self._tick())
        logger.info("🎮⚡ Manual tick triggered")

