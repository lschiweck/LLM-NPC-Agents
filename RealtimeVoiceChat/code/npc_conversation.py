"""
NPC-to-NPC Conversation Orchestrator

Manages conversations between NPCs, allowing them to talk to each other
independently of the player. Supports both two-character dialogues and
single-character monologues.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List, Any
from enum import Enum

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
        on_conversation_end: Optional[Callable[[List[ConversationTurn]], None]] = None
    ):
        """
        Initialize the orchestrator.
        
        Args:
            get_pipeline: Function to get a SpeechPipelineManager by character ID
            on_turn_complete: Callback when a turn is complete (turn, audio_bytes)
            on_state_update: Callback when state changes
            on_conversation_end: Callback when conversation finishes
        """
        self.get_pipeline = get_pipeline
        self.on_turn_complete = on_turn_complete
        self.on_state_update = on_state_update
        self.on_conversation_end = on_conversation_end
        
        self.state = NPCConversationState()
        self._stop_requested = False
        self._conversation_task: Optional[asyncio.Task] = None
        
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
        
        logger.info(f"🎭 Starting NPC conversation: {config.npc1_id} {'↔ ' + config.npc2_id if config.npc2_id else '(monologue)'}, {config.total_turns} turns")
        self._notify_state_update()
        
        # Start conversation loop in background
        self._conversation_task = asyncio.create_task(self._conversation_loop())
        return True
    
    def stop_conversation(self):
        """Request the conversation to stop."""
        if self.state.state == ConversationState.RUNNING:
            logger.info("🎭 Stop requested for NPC conversation")
            self._stop_requested = True
            self.state.state = ConversationState.STOPPING
            self._notify_state_update()
    
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
                
                # Generate the response
                turn = await self._generate_turn(
                    speaker_id=speaker_id,
                    listener_id=listener_id,
                    is_first_turn=self.state.current_turn == 1,
                    is_last_turn=is_last_turn,
                    context=config.context if self.state.current_turn == 1 else None
                )
                
                if turn is None:
                    logger.error("🎭 Failed to generate turn")
                    self.state.error = "Failed to generate response"
                    break
                
                # Add to history
                self.state.conversation_history.append(turn)
                
                # Update inter-NPC history for both characters
                await self._update_inter_npc_history(speaker_id, listener_id, turn.message)
                
                # Generate TTS and notify
                audio_bytes = None
                try:
                    audio_bytes = await self._generate_tts(speaker_id, turn.message)
                except Exception as e:
                    logger.error(f"🎭 TTS failed for {speaker_id}: {e}")
                
                # Notify even if TTS failed (text still shows)
                if self.on_turn_complete:
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
                
                # Small delay between turns for natural pacing
                if self.state.turns_remaining > 0 and not self._stop_requested:
                    logger.info(f"🎭 Waiting 0.5s before next turn...")
                    await asyncio.sleep(0.5)
            
            # Conversation finished
            self.state.state = ConversationState.FINISHED
            logger.info(f"🎭 NPC conversation finished after {len(self.state.conversation_history)} turns")
            
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
        
        # Build the prompt for this turn
        prompt_parts = []
        
        # Add conversation context for first turn
        if is_first_turn and context:
            if listener_id:
                prompt_parts.append(f"[Start a conversation with {listener_id} about: {context}]")
            else:
                prompt_parts.append(f"[Make a statement about: {context}]")
        
        # Add what the other person just said (if not first turn and not monologue)
        if not is_first_turn and listener_id and self.state.conversation_history:
            last_turn = self.state.conversation_history[-1]
            prompt_parts.append(f"{last_turn.speaker_id} said: \"{last_turn.message}\"")
        
        # Add hint for last turn
        if is_last_turn:
            prompt_parts.append("[This is your last response in this conversation - wrap it up naturally]")
        
        # Combine prompt
        prompt = " ".join(prompt_parts) if prompt_parts else "[Continue the conversation]"
        
        logger.info(f"🎭 Generating response for {speaker_id}: {prompt[:100]}...")
        
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
        """Generate LLM response (runs in thread)."""
        try:
            logger.info(f"🎭 LLM generating for {speaker_id}: {prompt[:80]}...")
            
            # Build a temporary history for this inter-NPC conversation
            history = []
            
            # Add recent conversation turns as context
            for turn in self.state.conversation_history[-6:]:  # Last 6 turns for context
                if turn.speaker_id == speaker_id:
                    history.append({"role": "assistant", "content": turn.message})
                else:
                    history.append({"role": "user", "content": f"{turn.speaker_id}: {turn.message}"})
            
            logger.info(f"🎭 History for {speaker_id}: {len(history)} messages")
            
            # Generate response - run directly without semaphore
            # NPC conversations run independently of player conversations
            full_response = ""
            for chunk in pipeline.llm.generate(text=prompt, history=history, use_system_prompt=True):
                full_response += chunk
            
            logger.info(f"🎭 LLM response for {speaker_id}: {full_response[:100]}...")
            
            # Clean up the response
            full_response = full_response.strip()
            if full_response.startswith('"') and full_response.endswith('"'):
                full_response = full_response[1:-1]
            
            return full_response
            
        except Exception as e:
            logger.error(f"🎭 LLM generation error for {speaker_id}: {e}", exc_info=True)
            return None
    
    async def _generate_tts(self, speaker_id: str, text: str) -> Optional[bytes]:
        """Generate TTS audio for the turn."""
        pipeline = self.get_pipeline(speaker_id)
        if not pipeline:
            logger.warning(f"🎭 TTS: No pipeline for {speaker_id}")
            return None
        if not hasattr(pipeline, 'audio') or not pipeline.audio:
            logger.warning(f"🎭 TTS: No audio processor for {speaker_id}")
            return None
        
        def generate_audio_sync():
            """Generate audio synchronously using AudioProcessor."""
            import threading
            from queue import Queue, Empty
            
            try:
                logger.info(f"🎭 Generating TTS for {speaker_id}: {text[:50]}...")
                
                # Create a queue and stop event for the synthesize method
                audio_queue = Queue()
                stop_event = threading.Event()
                
                # Run synthesis in current thread - it will put chunks into the queue
                # We need to run this and collect from queue
                audio_chunks = []
                
                def collect_chunks():
                    """Collect chunks from queue until synthesis completes."""
                    while True:
                        try:
                            chunk = audio_queue.get(timeout=0.1)
                            if chunk is None:  # End signal
                                break
                            if chunk:
                                audio_chunks.append(chunk)
                        except Empty:
                            if stop_event.is_set():
                                break
                            continue
                
                # Start collector in a thread
                collector = threading.Thread(target=collect_chunks, daemon=True)
                collector.start()
                
                # Run synthesis (blocking)
                completed = pipeline.audio.synthesize(
                    text=text,
                    audio_chunks=audio_queue,
                    stop_event=stop_event,
                    generation_string=f"npc_conv_{speaker_id}"
                )
                
                # Signal end and wait for collector
                audio_queue.put(None)
                collector.join(timeout=5.0)
                
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
            return await asyncio.to_thread(generate_audio_sync)
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

