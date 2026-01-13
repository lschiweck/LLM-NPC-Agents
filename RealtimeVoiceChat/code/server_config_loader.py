# server_config_loader.py
"""
Centralized configuration loader for the voice chat server.

Loads server_config.json and provides typed access to all settings.
Falls back to sensible defaults if config file is missing or incomplete.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).resolve().parent / "server_config.json"


@dataclass
class ServerSettings:
    """Server network settings."""
    host: str = "0.0.0.0"
    port: int = 8000
    use_ssl: bool = False


@dataclass
class InitializationSettings:
    """Pipeline initialization settings."""
    pre_init_pipelines: bool = False
    pre_init_mode: str = "all"  # "all", "specific", or "none"
    pre_init_character_ids: List[str] = None
    
    def __post_init__(self):
        if self.pre_init_character_ids is None:
            self.pre_init_character_ids = []
        # Normalize mode
        self.pre_init_mode = self.pre_init_mode.lower() if self.pre_init_mode else "all"


@dataclass
class DefaultSettings:
    """Default TTS/LLM settings."""
    tts_engine: str = "kokoro"
    orpheus_model: str = "orpheus-3b-0.1-ft-Q8_0-GGUF/orpheus-3b-0.1-ft-q8_0.gguf"
    llm_provider: str = "ollama"
    llm_model: str = "llama3"
    no_think: bool = False
    language: str = "en"


@dataclass
class LoggingSettings:
    """Conversation logging settings."""
    enabled: bool = True
    output_dir: str = "./conversation_logs"
    log_player_utterances: bool = True
    log_npc_responses: bool = True
    log_processing_times: bool = True
    log_injections: bool = True
    log_npc_conversations: bool = True
    log_director_notes: bool = True
    log_format: str = "jsonl"


@dataclass
class ServerConfig:
    """Complete server configuration."""
    server: ServerSettings
    initialization: InitializationSettings
    defaults: DefaultSettings
    logging: LoggingSettings
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServerConfig":
        """Create ServerConfig from dictionary."""
        return cls(
            server=ServerSettings(**data.get("server", {})),
            initialization=InitializationSettings(**{
                k: v for k, v in data.get("initialization", {}).items() 
                if k != "comment"
            }),
            defaults=DefaultSettings(**data.get("defaults", {})),
            logging=LoggingSettings(**{
                k: v for k, v in data.get("logging", {}).items() 
                if k != "comment"
            }),
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        from dataclasses import asdict
        return {
            "server": asdict(self.server),
            "initialization": asdict(self.initialization),
            "defaults": asdict(self.defaults),
            "logging": asdict(self.logging),
        }


# Global config instance
_config: Optional[ServerConfig] = None


def load_server_config(config_path: Optional[Path] = None) -> ServerConfig:
    """
    Load server configuration from JSON file.
    
    Args:
        config_path: Optional path to config file. Uses default if not provided.
        
    Returns:
        ServerConfig instance with all settings.
    """
    global _config
    
    path = config_path or CONFIG_FILE
    
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _config = ServerConfig.from_dict(data)
            logger.info(f"⚙️ Loaded server config from {path}")
        except Exception as e:
            logger.warning(f"⚙️⚠️ Failed to load config from {path}: {e}. Using defaults.")
            _config = ServerConfig.from_dict({})
    else:
        logger.info(f"⚙️ Config file not found at {path}. Using defaults.")
        _config = ServerConfig.from_dict({})
        
        # Create default config file
        try:
            default_config = {
                "server": {"host": "0.0.0.0", "port": 8000, "use_ssl": False},
                "initialization": {
                    "pre_init_pipelines": False,
                    "pre_init_character_ids": [],
                    "comment": "Set pre_init_pipelines to true to load models at startup"
                },
                "defaults": {
                    "tts_engine": "kokoro",
                    "orpheus_model": "orpheus-3b-0.1-ft-Q8_0-GGUF/orpheus-3b-0.1-ft-q8_0.gguf",
                    "llm_provider": "ollama",
                    "llm_model": "llama3",
                    "no_think": False,
                    "language": "en"
                },
                "logging": {
                    "enabled": True,
                    "output_dir": "./conversation_logs",
                    "log_player_utterances": True,
                    "log_npc_responses": True,
                    "log_processing_times": True,
                    "log_injections": True,
                    "log_npc_conversations": True,
                    "log_director_notes": True,
                    "log_format": "jsonl",
                    "comment": "Set enabled to false to disable conversation logging"
                },
                "game_manager": {
                    "config_file": "game_manager_config.json"
                }
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_config, f, indent=2)
            logger.info(f"⚙️ Created default config file at {path}")
        except Exception as e:
            logger.warning(f"⚙️⚠️ Could not create default config: {e}")
    
    return _config


def get_server_config() -> ServerConfig:
    """
    Get the current server configuration.
    
    Loads config if not already loaded.
    
    Returns:
        ServerConfig instance.
    """
    global _config
    if _config is None:
        _config = load_server_config()
    return _config


def reload_server_config() -> ServerConfig:
    """
    Reload the server configuration from file.
    
    Returns:
        Fresh ServerConfig instance.
    """
    global _config
    _config = None
    return load_server_config()
