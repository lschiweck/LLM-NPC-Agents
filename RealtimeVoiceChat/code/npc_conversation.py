"""
NPC-to-NPC Conversation Orchestrator

Manages conversations between NPCs, allowing them to talk to each other
independently of the player. Supports both two-character dialogues and
single-character monologues.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List, Any
from enum import Enum

from conversation_logger import get_conversation_logger

logger = logging.getLogger(__name__)


class ConversationState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    FINISHED = "finished"


@dataclass
class ConversationTurn:
    """Represents a single turn in an NPC conversation."""
    speaker_id: str
    message: str
    turn_number: int
    is_last: bool = False


@dataclass 
class NPCConversationConfig:
    """Configuration for an NPC conversation."""
    npc1_id: str
    npc2_id: Optional[str]  # None for monologue
    total_turns: int
    context: str = ""
    
    @property
    def is_monologue(self) -> bool:
        return self.npc2_id is None or self.npc2_id == ""


@dataclass
class NPCConversationState:
    """Current state of an NPC conversation."""
    state: ConversationState = ConversationState.IDLE
    config: Optional[NPCConversationConfig] = None
    current_turn: int = 0
    turns_remaining: int = 0
    current_speaker: Optional[str] = None
    conversation_history: List[ConversationTurn] = field(default_factory=list)
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "config": {
                "npc1_id": self.config.npc1_id if self.config else None,
                "npc2_id": self.config.npc2_id if self.config else None,
                "total_turns": self.config.total_turns if self.config else 0,
                "context": self.config.context if self.config else "",
                "is_monologue": self.config.is_monologue if self.config else False,
            } if self.config else None,
            "current_turn": self.current_turn,
            "turns_remaining": self.turns_remaining,
            "current_speaker": self.current_speaker,
            "conversation_history": [
                {
                    "speaker_id": t.speaker_id,
                    "message": t.message,
                    "turn_number": t.turn_number,
                    "is_last": t.is_last
                }
                for t in self.conversation_history
            ],
            "error": self.error
        }


class NPCConversationOrchestrator:
    """
    Orchestrates conversations between NPCs.
    
    Manages turn-taking, context injection, and coordinates with
    SpeechPipelineManagers for TTS generation.
    """
    
    def __init__(
        self,
        get_pipeline: Callable[[str], Any],
        on_turn_complete: Optional[Callable[[ConversationTurn, bytes], None]] = None,
        on_state_update: Optional[Callable[[NPCConversationState], None]] = None,
        on_conversation_end: Optional[Callable[[List[ConversationTurn]], None]] = None,
        on_conversation_interrupted: Optional[Callable[[], None]] = None
    ):
        """
        Initialize the orchestrator.
        
        Args:
            get_pipeline: Function to get a SpeechPipelineManager by character ID
            on_turn_complete: Callback when a turn is complete (turn, audio_bytes)
            on_state_update: Callback when state changes
            on_conversation_end: Callback when conversation finishes
            on_conversation_interrupted: Callback when conversation is interrupted by player
        """
        self.get_pipeline = get_pipeline
        self.on_turn_complete = on_turn_complete
        self.on_state_update = on_state_update
        self.on_conversation_end = on_conversation_end
        self.on_conversation_interrupted = on_conversation_interrupted
        
        self.state = NPCConversationState()
        self._stop_requested = False
        self._conversation_task: Optional[asyncio.Task] = None
        self._interrupt_event = asyncio.Event()  # For immediate interrupt signaling
        
    def _notify_state_update(self):
        """Notify listeners of state change."""
        if self.on_state_update:
            try:
                self.on_state_update(self.state)
            except Exception as e:
                logger.error(f"Error in state update callback: {e}")
    
    async def start_conversation(self, config: NPCConversationConfig) -> bool:
        """
        Start a new NPC conversation.
        
        Args:
            config: Configuration for the conversation
            
        Returns:
            True if conversation started successfully
        """
        if self.state.state == ConversationState.RUNNING:
            logger.warning("Cannot start conversation - one is already running")
            return False
        
        # Validate NPCs exist
        pipeline1 = self.get_pipeline(config.npc1_id)
        if not pipeline1:
            self.state.error = f"NPC '{config.npc1_id}' not found"
            self._notify_state_update()
            return False
            
        if not config.is_monologue:
            pipeline2 = self.get_pipeline(config.npc2_id)
            if not pipeline2:
                self.state.error = f"NPC '{config.npc2_id}' not found"
                self._notify_state_update()
                return False
        
        # Initialize state
        self.state = NPCConversationState(
            state=ConversationState.RUNNING,
            config=config,
            current_turn=1,
            turns_remaining=config.total_turns,
            current_speaker=config.npc1_id,
            conversation_history=[],
            error=None
        )
        self._stop_requested = False
        self._interrupt_event.clear()  # Reset interrupt signal
        
        logger.info(f"🎭 Starting NPC conversation: {config.npc1_id} {'↔ ' + config.npc2_id if config.npc2_id else '(monologue)'}, {config.total_turns} turns")
        self._notify_state_update()
        
        # Log conversation start
        conv_logger = get_conversation_logger()
        if conv_logger:
            participants = [config.npc1_id]
            if config.npc2_id:
                participants.append(config.npc2_id)
            conv_logger.log_npc_conversation_start(
                participants=participants,
                context=config.context,
                total_turns=config.total_turns
            )
        
        # Start conversation loop in background
        self._conversation_task = asyncio.create_task(self._conversation_loop())
        return True
    
    def stop_conversation(self, reason: str = "manual"):
        """
        Request the conversation to stop immediately.
        
        Args:
            reason: Why the conversation is being stopped (e.g., "player_speaking", "manual")
        """
        if self.state.state == ConversationState.RUNNING:
            logger.info(f"🎭 Stop requested for NPC conversation (reason: {reason})")
            self._stop_requested = True
            self._interrupt_event.set()  # Signal immediate interruption
            self.state.state = ConversationState.STOPPING
            self._notify_state_update()
            
            # Notify clients to stop audio playback immediately
            if self.on_conversation_interrupted:
                try:
                    self.on_conversation_interrupted()
                except Exception as e:
                    logger.error(f"🎭 Error in conversation interrupted callback: {e}")
    
    async def _conversation_loop(self):
        """Main conversation loop."""
        config = self.state.config
        
        try:
            while self.state.turns_remaining > 0 and not self._stop_requested:
                is_last_turn = self.state.turns_remaining == 1
                speaker_id = self.state.current_speaker
                
                logger.info(f"🎭 === TURN START === Remaining: {self.state.turns_remaining}, Speaker: {speaker_id}")
                
                # Determine the other speaker (listener)
                if config.is_monologue:
                    listener_id = None
                else:
                    listener_id = config.npc2_id if speaker_id == config.npc1_id else config.npc1_id
                
                logger.info(f"🎭 Turn {self.state.current_turn}/{config.total_turns}: {speaker_id} speaks" + 
                           (f" (last turn)" if is_last_turn else "") + f", listener: {listener_id}")
                
                # Check for interruption before generating
                if self._stop_requested:
                    logger.info("🎭 Conversation interrupted before LLM generation")
                    break
                
                # Generate the response (with timing)
                llm_start = time.time()
                turn = await self._generate_turn(
                    speaker_id=speaker_id,
                    listener_id=listener_id,
                    is_first_turn=self.state.current_turn == 1,
                    is_last_turn=is_last_turn,
                    context=config.context if self.state.current_turn == 1 else None
                )
                llm_time_ms = (time.time() - llm_start) * 1000
                
                # Check if interrupted during LLM generation
                if self._stop_requested:
                    logger.info("🎭 Conversation interrupted during LLM generation")
                    break
                
                if turn is None:
                    logger.error("🎭 Failed to generate turn")
                    self.state.error = "Failed to generate response"
                    break
                
                # Add to history
                self.state.conversation_history.append(turn)
                
                # Update inter-NPC history for both characters
                await self._update_inter_npc_history(speaker_id, listener_id, turn.message)
                
                # Check for interruption before TTS
                if self._stop_requested:
                    logger.info("🎭 Conversation interrupted before TTS generation")
                    break
                
                # Generate TTS and notify (with timing)
                tts_start = time.time()
                audio_bytes = None
                try:
                    audio_bytes = await self._generate_tts(speaker_id, turn.message)
                except Exception as e:
                    logger.error(f"🎭 TTS failed for {speaker_id}: {e}")
                tts_time_ms = (time.time() - tts_start) * 1000
                
                # Check if interrupted during TTS generation - don't broadcast if so
                if self._stop_requested:
                    logger.info("🎭 Conversation interrupted during TTS - not broadcasting turn")
                    break
                
                # Log the turn
                conv_logger = get_conversation_logger()
                if conv_logger:
                    conv_logger.log_npc_conversation_turn(
                        speaker_id=speaker_id,
                        listener_id=listener_id,
                        message=turn.message,
                        turn_number=self.state.current_turn,
                        context=config.context if self.state.current_turn == 1 else None,
                        llm_time_ms=llm_time_ms,
                        tts_time_ms=tts_time_ms
                    )
                
                # Notify even if TTS failed (text still shows) - but NOT if interrupted
                if self.on_turn_complete and not self._stop_requested:
                    try:
                        self.on_turn_complete(turn, audio_bytes)
                    except Exception as e:
                        logger.error(f"🎭 Turn complete callback error: {e}")
                
                # Update state for next turn
                self.state.turns_remaining -= 1
                self.state.current_turn += 1
                
                if not config.is_monologue and self.state.turns_remaining > 0:
                    # Alternate speaker
                    self.state.current_speaker = listener_id
                
                self._notify_state_update()
                
                logger.info(f"🎭 === TURN {self.state.current_turn - 1} COMPLETE === Next speaker: {self.state.current_speaker}, Remaining: {self.state.turns_remaining}")
                
                # Small delay between turns for natural pacing - but check for interrupt
                if self.state.turns_remaining > 0 and not self._stop_requested:
                    logger.info(f"🎭 Waiting 0.5s before next turn...")
                    # Use shorter sleep intervals so we can respond to interrupts faster
                    for _ in range(5):  # 5 x 0.1s = 0.5s total
                        if self._stop_requested:
                            logger.info("🎭 Conversation interrupted during pause")
                            break
                        await asyncio.sleep(0.1)
            
            # Conversation finished
            self.state.state = ConversationState.FINISHED
            logger.info(f"🎭 NPC conversation finished after {len(self.state.conversation_history)} turns")
            
            # Log conversation end
            conv_logger = get_conversation_logger()
            if conv_logger:
                participants = [config.npc1_id]
                if config.npc2_id:
                    participants.append(config.npc2_id)
                conv_logger.log_npc_conversation_end(
                    participants=participants,
                    turns_completed=len(self.state.conversation_history),
                    was_stopped=self._stop_requested
                )
            
            if self.on_conversation_end:
                try:
                    self.on_conversation_end(self.state.conversation_history)
                except Exception as e:
                    logger.error(f"Error in conversation end callback: {e}")
                    
        except Exception as e:
            logger.error(f"🎭 Error in conversation loop: {e}", exc_info=True)
            self.state.error = str(e)
            self.state.state = ConversationState.FINISHED
        
        self._notify_state_update()
    
    async def _generate_turn(
        self,
        speaker_id: str,
        listener_id: Optional[str],
        is_first_turn: bool,
        is_last_turn: bool,
        context: Optional[str]
    ) -> Optional[ConversationTurn]:
        """Generate a single conversation turn."""
        pipeline = self.get_pipeline(speaker_id)
        if not pipeline:
            return None

        def build_turn_prompt() -> str:
            """
            Build a prompt for NPC-to-NPC turns that strongly discourages
            screenplay-style multi-speaker output.
            """
            parts: List[str] = []

            # Hard constraints (we also enforce again via post-processing)
            parts.append(
                "IMPORTANT RULES (DO NOT SAY THESE OUT LOUD): "
                "Reply as ONE speaker only. Output ONLY what YOU say. "
                "Do NOT write the other person's dialogue. "
                "Do NOT include any name prefixes like 'Lisa:' or 'Paul:'. "
                "Keep it to 1–2 short sentences."
            )

            if listener_id:
                parts.append(f"You are {speaker_id}. You are talking to {listener_id}.")
            else:
                parts.append(f"You are {speaker_id}.")

            if is_first_turn and context:
                if listener_id:
                    parts.append(f"Start the conversation about: {context}")
                else:
                    parts.append(f"Make a short statement about: {context}")

            if not is_first_turn and listener_id and self.state.conversation_history:
                last_turn = self.state.conversation_history[-1]
                # Avoid speaker labels in the prompt to prevent scripted output
                parts.append(f'They just said: "{last_turn.message}"')

            if is_last_turn:
                parts.append("Wrap it up naturally in one short sentence.")

            return " ".join([p.strip() for p in parts if p and p.strip()])
        
        prompt = build_turn_prompt()
        
        logger.info(f"🎭 Generating response for {speaker_id}: {prompt[:120]}...")
        
        try:
            # Use the pipeline's LLM to generate response
            # We need to generate without going through the full speech pipeline
            # Just use the LLM directly
            response = await asyncio.to_thread(
                self._generate_llm_response,
                pipeline,
                prompt,
                listener_id,
                speaker_id
            )
            
            if response:
                return ConversationTurn(
                    speaker_id=speaker_id,
                    message=response,
                    turn_number=self.state.current_turn,
                    is_last=is_last_turn
                )
        except Exception as e:
            logger.error(f"🎭 Error generating turn: {e}", exc_info=True)
        
        return None
    
    def _generate_llm_response(
        self,
        pipeline,
        prompt: str,
        listener_id: Optional[str],
        speaker_id: str
    ) -> Optional[str]:
        """Generate LLM response (runs in thread). Returns None if interrupted."""
        # Check for interruption before starting
        if self._stop_requested:
            logger.info(f"🎭 LLM generation cancelled for {speaker_id} - stop requested")
            return None
            
        try:
            logger.info(f"🎭 LLM generating for {speaker_id}: {prompt[:80]}...")

            def _truncate_sentences(text: str, max_sentences: int = 2) -> str:
                t = re.sub(r"\s+", " ", (text or "").strip())
                if not t:
                    return ""
                # Split on sentence boundaries; keep punctuation.
                parts = re.split(r"(?<=[.!?])\s+", t)
                parts = [p.strip() for p in parts if p and p.strip()]
                if not parts:
                    return t
                return " ".join(parts[:max_sentences]).strip()

            def _strip_meta_prefix(text: str) -> str:
                """
                Strip screenplay/meta prefixes that should never be spoken, e.g.:
                - [Director's note] ...
                - (aside) ...
                - Director's note: ...
                - Note: ...
                Only applies to the *start* of the utterance; keeps the actual line.
                """
                t = (text or "").strip()
                if not t:
                    return ""

                # Remove common "meta" leading tags in brackets/parentheses (repeat a few times)
                # Examples: [Director's note], [Directors note], [aside], [laughs], (whispers)
                for _ in range(4):
                    new_t = re.sub(r"^\s*[\[\(]\s*[^]\)]{1,120}\s*[\]\)]\s*", "", t)
                    if new_t == t:
                        break
                    t = new_t.strip()

                # Remove explicit meta prefixes
                t = re.sub(r"^\s*(director'?s\s*note|directors\s*note|note|narrator|stage\s*direction)\s*:\s*",
                           "",
                           t,
                           flags=re.IGNORECASE)

                return t.strip()

            def _sanitize_single_speaker(text: str) -> str:
                """
                Remove scripted multi-speaker formatting.
                Keep ONLY the first speaker's utterance and strip any name prefixes.
                """
                t = (text or "").strip()
                if not t:
                    return ""

                # Strip surrounding quotes
                if (t.startswith('"') and t.endswith('"')) or (t.startswith("“") and t.endswith("”")):
                    t = t[1:-1].strip()

                # Strip meta prefixes like [Director's note] before we cut/label-strip
                t = _strip_meta_prefix(t)

                # Cut at first newline (screenplay formatting often uses multiple lines)
                t = t.splitlines()[0].strip()

                # If model wrote multiple speakers inline, cut at the first occurrence of any speaker label
                # for the *other* character.
                if listener_id:
                    # e.g. "PaulAdams: ... LisaParker: ..."
                    idx = t.lower().find(f"{listener_id.lower()}:")
                    if idx != -1:
                        t = t[:idx].strip()

                # Remove leading "Speaker:" labels (either speaker_id or generic)
                t = re.sub(rf"^\s*{re.escape(speaker_id)}\s*:\s*", "", t, flags=re.IGNORECASE)
                t = re.sub(r"^\s*[A-Za-z0-9_]+\s*:\s*", "", t)

                # If the listener label still appears later, cut before it (fallback)
                if listener_id:
                    m = re.search(rf"\b{re.escape(listener_id)}\s*:", t, flags=re.IGNORECASE)
                    if m:
                        t = t[: m.start()].strip()

                # Final pass: strip any leftover meta prefix after label removal
                t = _strip_meta_prefix(t)

                return t.strip()
            
            # Build a temporary history for this inter-NPC conversation.
            # IMPORTANT: Do NOT prefix turns with speaker IDs here. That nudges the model into
            # screenplay-style outputs ("Paul: ... Lisa: ...") in a single completion.
            history = []
            
            # FIRST: Add context about what this NPC told the detective (player)
            # This ensures NPCs remember their player conversations when talking to each other
            if hasattr(pipeline, 'history') and pipeline.history:
                player_context_parts = []
                for msg in pipeline.history[-8:]:  # Last 8 messages with player
                    if msg.get("role") == "assistant":
                        player_context_parts.append(f"You said: {msg['content']}")
                    else:
                        player_context_parts.append(f"Detective asked: {msg['content']}")
                
                if player_context_parts:
                    player_summary = "[CONTEXT - Your recent conversation with the detective. Remember this and stay consistent:]\n" + "\n".join(player_context_parts)
                    history.append({"role": "user", "content": player_summary})
                    history.append({"role": "assistant", "content": "I remember what I told the detective and will stay consistent."})
            
            # THEN: Add recent NPC-to-NPC conversation turns as context
            for turn in self.state.conversation_history[-6:]:  # Last 6 turns for context
                if turn.speaker_id == speaker_id:
                    history.append({"role": "assistant", "content": turn.message})
                else:
                    # Treat the other NPC as the "user" without labels.
                    history.append({"role": "user", "content": turn.message})
            
            logger.info(f"🎭 History for {speaker_id}: {len(history)} messages")
            
            # Generate response - run directly without semaphore
            # NPC conversations run independently of player conversations
            # Keep NPC-to-NPC turns short and snappy.
            # Ollama supports num_predict; OpenAI/LMStudio will ignore unknown options safely.
            full_response = ""
            for chunk in pipeline.llm.generate(
                text=prompt,
                history=history,
                use_system_prompt=True,
                num_predict=90,
                temperature=0.6,
                stop=[
                    "\n",
                    f"{speaker_id}:",
                    f"{listener_id}:" if listener_id else "",
                ],
            ):
                # Check for interruption during generation
                if self._stop_requested:
                    logger.info(f"🎭 LLM generation interrupted for {speaker_id}")
                    return None
                full_response += chunk
            
            logger.info(f"🎭 LLM response for {speaker_id}: {full_response[:100]}...")
            
            # Clean + hard-enforce single-speaker short output.
            cleaned = _sanitize_single_speaker(full_response)
            cleaned = _truncate_sentences(cleaned, max_sentences=2)

            logger.info(f"🎭 Cleaned response for {speaker_id}: {cleaned[:120]}...")
            return cleaned.strip()
            
        except Exception as e:
            logger.error(f"🎭 LLM generation error for {speaker_id}: {e}", exc_info=True)
            return None
    
    async def _generate_tts(self, speaker_id: str, text: str) -> Optional[bytes]:
        """Generate TTS audio for the turn. Returns None if interrupted."""
        # Check for interruption before starting
        if self._stop_requested:
            logger.info(f"🎭 TTS generation cancelled for {speaker_id} - stop requested")
            return None
            
        pipeline = self.get_pipeline(speaker_id)
        if not pipeline:
            logger.warning(f"🎭 TTS: No pipeline for {speaker_id}")
            return None
        if not hasattr(pipeline, 'audio') or not pipeline.audio:
            logger.warning(f"🎭 TTS: No audio processor for {speaker_id}")
            return None
        
        # Create a stop event that we can trigger from outside
        import threading
        tts_stop_event = threading.Event()
        
        def generate_audio_sync():
            """Generate audio synchronously using AudioProcessor."""
            from queue import Queue, Empty
            
            try:
                logger.info(f"🎭 Generating TTS for {speaker_id}: {text[:50]}...")
                
                # Create a queue for the synthesize method
                audio_queue = Queue()
                
                # Run synthesis in current thread - it will put chunks into the queue
                # We need to run this and collect from queue
                audio_chunks = []
                
                def collect_chunks():
                    """Collect chunks from queue until synthesis completes or interrupted."""
                    while True:
                        # Check for interruption
                        if self._stop_requested or tts_stop_event.is_set():
                            logger.info(f"🎭 TTS collection interrupted for {speaker_id}")
                            break
                        try:
                            chunk = audio_queue.get(timeout=0.1)
                            if chunk is None:  # End signal
                                break
                            if chunk:
                                audio_chunks.append(chunk)
                        except Empty:
                            if tts_stop_event.is_set():
                                break
                            continue
                
                # Start collector in a thread
                collector = threading.Thread(target=collect_chunks, daemon=True)
                collector.start()
                
                # Run synthesis (blocking) - pass the stop event so it can be interrupted
                completed = pipeline.audio.synthesize(
                    text=text,
                    audio_chunks=audio_queue,
                    stop_event=tts_stop_event,
                    generation_string=f"npc_conv_{speaker_id}"
                )
                
                # Signal end and wait for collector
                audio_queue.put(None)
                collector.join(timeout=5.0)
                
                # Check if we were interrupted
                if self._stop_requested:
                    logger.info(f"🎭 TTS interrupted during synthesis for {speaker_id}")
                    return None
                
                if audio_chunks:
                    audio_data = b''.join(audio_chunks)
                    logger.info(f"🎭 TTS generated {len(audio_data)} bytes for {speaker_id} (completed={completed})")
                    return audio_data
                else:
                    logger.warning(f"🎭 TTS: No audio chunks generated for {speaker_id}")
                    return None
                    
            except Exception as e:
                logger.error(f"🎭 TTS sync error for {speaker_id}: {e}", exc_info=True)
                return None
        
        try:
            # Check periodically if we should abort while waiting for TTS
            result = await asyncio.to_thread(generate_audio_sync)
            
            # Final check for interruption
            if self._stop_requested:
                logger.info(f"🎭 TTS result discarded for {speaker_id} - stop requested")
                return None
                
            return result
        except Exception as e:
            logger.error(f"🎭 TTS generation error for {speaker_id}: {e}", exc_info=True)
            return None
    
    async def _update_inter_npc_history(
        self,
        speaker_id: str,
        listener_id: Optional[str],
        message: str
    ):
        """Update the inter-NPC history for both characters."""
        if not listener_id:
            return  # Monologue, no listener to update
        
        # Get both pipelines
        speaker_pipeline = self.get_pipeline(speaker_id)
        listener_pipeline = self.get_pipeline(listener_id)
        
        # Update speaker's inter-NPC history (they said this)
        if speaker_pipeline:
            if not hasattr(speaker_pipeline, 'inter_npc_history'):
                speaker_pipeline.inter_npc_history = {}
            if listener_id not in speaker_pipeline.inter_npc_history:
                speaker_pipeline.inter_npc_history[listener_id] = []
            speaker_pipeline.inter_npc_history[listener_id].append({
                "role": "assistant",  # Speaker's perspective: they said it
                "content": message
            })
        
        # Update listener's inter-NPC history (they heard this)
        if listener_pipeline:
            if not hasattr(listener_pipeline, 'inter_npc_history'):
                listener_pipeline.inter_npc_history = {}
            if speaker_id not in listener_pipeline.inter_npc_history:
                listener_pipeline.inter_npc_history[speaker_id] = []
            listener_pipeline.inter_npc_history[speaker_id].append({
                "role": "user",  # Listener's perspective: they heard it
                "content": message
            })
    
    def get_state(self) -> NPCConversationState:
        """Get current conversation state."""
        return self.state
    
    def reset(self):
        """Reset the orchestrator to idle state."""
        if self._conversation_task and not self._conversation_task.done():
            self._conversation_task.cancel()
        
        self.state = NPCConversationState()
        self._stop_requested = False
        self._notify_state_update()

