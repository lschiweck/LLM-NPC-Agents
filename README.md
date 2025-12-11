# LLM NPCs – Intelligent Context-Aware AI Agents for Unity

**Note:** This project is a **modified version** of [KoljaB/RealtimeVoiceChat](https://github.com/KoljaB/RealtimeVoiceChat).  
The technical **core (audio streaming, STT, TTS, WebSocket structure)** is based on that project, but the backend has been significantly expanded to support **multi-NPC interaction**, **Game Manager story orchestration**, and **dynamic context injection**.

---

**Intelligent agent-based NPCs for Unity** – Create context-aware characters that dynamically respond to game events, follow evolving storylines, and interact naturally through real-time voice conversation.

![LLM NPCs Demo](example.png)

## What This Project Does

This system enables **intelligent NPC agents** in Unity that go beyond simple chatbots:

- 🎮 **Game Manager AI** – An invisible "game master" that orchestrates the story in the background, analyzes player actions, and dynamically injects instructions into NPCs to shape their behavior
- 🧠 **Context-Aware NPCs** – Characters that understand game state, react to player discoveries, and adapt their responses based on injected game context
- 🎭 **Multi-NPC Orchestration** – Run multiple independent characters in parallel, each with their own personality, knowledge, and role in the story
- 🎤 **Real-Time Voice** – Natural speech input and synthesized voice output with minimal latency

**Example use case**: A detective game where the Game Manager tracks what evidence the player finds, then injects nervousness into the guilty NPC when relevant topics come up – all happening dynamically without scripted dialogue trees.

### Key Capabilities

Via the Unity client, NPCs can be:
- **Specifically addressed** – Talk to individual characters
- **Dynamically activated** – Trigger conversations based on proximity or events
- **Combined** – Enable dialogues between NPCs, player moderation, role changes
- **Context-injected** – Feed game events to characters or the Game Manager in real-time

This makes the project ideal for **VR/game scenarios** requiring an **ensemble of intelligent NPCs** that dynamically respond to an evolving narrative.

> ⚠️ **Important:** This project provides the framework and architecture for intelligent NPCs, but **prompt engineering is required** to make it work well for your specific use case. You will need to heavily customize the system prompts, Game Manager instructions, and character configurations to fit your game's narrative and desired NPC behaviors. The included prompts serve as examples and starting points.

---

The system consists of two main components:

1. **Backend (Python/FastAPI)** – Speech-to-Text, LLM inference, Text-to-Speech
2. **Unity Client (C#)** – Integration into VR/3D applications

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [System Requirements](#system-requirements)
- [Installation](#installation)
  - [Option A: Conda Environment (recommended for development)](#option-a-conda-environment-recommended-for-development)
  - [Option B: Docker (recommended for deployment)](#option-b-docker-recommended-for-deployment)
- [Setting Up Ollama](#setting-up-ollama)
- [Starting the Server](#starting-the-server)
- [Character Configuration](#character-configuration)
- [🎮 Game Manager LLM](#-game-manager-llm)
- [💉 System Prompt Injection](#-system-prompt-injection)
- [⚡ Performance: Global Generation Lock](#-performance-global-generation-lock)
- [Unity Integration](#unity-integration)
  - [Prerequisites](#prerequisites)
  - [Script Overview](#script-overview)
  - [Setup in Unity](#setup-in-unity)
  - [Example: Creating Your Own Character](#example-creating-your-own-character)
- [Data Flow & Protocol](#data-flow--protocol)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              UNITY CLIENT                                    │
│  ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────────┐  │
│  │ LiveLlmManager  │───▶│ LiveLlmCharacter │───▶│ AudioSource (Playback)  │  │
│  │ (Microphone)    │    │ (WebSocket)      │    │                         │  │
│  └────────┬────────┘    └──────────────────┘    └─────────────────────────┘  │
│           │                      ▲                                           │
└───────────┼──────────────────────┼───────────────────────────────────────────┘
            │ PCM Audio (48kHz)    │ TTS Chunks (Base64)
            ▼                      │
┌─────────────────────────────────────────────────────────────────────────────┐
│                           PYTHON BACKEND                                    │
│  ┌─────────────┐   ┌──────────────────┐   ┌─────────────────────────────┐   │
│  │ FastAPI     │   │ AudioInput       │   │ SpeechPipelineManager       │   │
│  │ WebSocket   │──▶│ Processor        │──▶│                             │   │
│  │ Server      │   │ (RealtimeSTT)    │   │ ┌─────────┐ ┌─────────────┐ │   │
│  └─────────────┘   └──────────────────┘   │ │ LLM     │ │ TTS         │ │   │
│                                           │ │ (Ollama)│ │ (Kokoro/    │ │   │
│                                           │ └─────────┘ │  Coqui)     │ │   │
│                                           │             └─────────────┘ │   │
│                                           └─────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Conversation Flow

1. **Voice Recording**: Unity captures microphone audio (48kHz, Mono, 16-bit PCM)
2. **Streaming**: Audio chunks are sent to the server via WebSocket
3. **Speech-to-Text**: `RealtimeSTT` converts speech to text (Whisper-based)
4. **LLM Inference**: The text is sent to Ollama/OpenAI, response is streamed
5. **Text-to-Speech**: `RealtimeTTS` synthesizes the response (Kokoro/Coqui/Orpheus)
6. **Audio Streaming**: TTS chunks are sent back as Base64-encoded PCM
7. **Playback**: Unity decodes and plays back the response

---

## System Requirements

| Component | Requirement |
|-----------|-------------|
| **Operating System** | Windows 10/11, Linux (Ubuntu 22.04+) |
| **Python** | 3.10 or 3.11 (not 3.12+) |
| **GPU** | NVIDIA with CUDA 12.1+ (recommended, minimum 8GB VRAM) |
| **RAM** | Minimum 16GB |
| **Unity** | 2021.3 LTS or newer |

> ⚠️ **Important**: Without an NVIDIA GPU, performance is significantly limited. STT and TTS will run on CPU, resulting in noticeable latency.

---

## Installation

### Option A: Conda Environment (recommended for development)

#### 1. Create Conda Environment

```powershell
# Create new environment with Python 3.10
conda create -n realtime-voice python=3.10 -y

# Activate environment
conda activate realtime-voice
```

#### 2. Navigate to the Code Directory

```powershell
cd RealtimeVoiceChat/code
```

#### 3. Install PyTorch (GPU Version)

```powershell
# For NVIDIA GPU with CUDA 12.1
pip install torch==2.5.1+cu121 torchaudio==2.5.1+cu121 torchvision --index-url https://download.pytorch.org/whl/cu121
```

For other CUDA versions see: https://pytorch.org/get-started/previous-versions/

#### 4. Install Dependencies

```powershell
pip install -r requirements.txt
```

#### 5. Environment for Later Use

```powershell
# To reactivate:
conda activate realtime-voice
cd RealtimeVoiceChat/code
```

---

### Option B: Docker (recommended for deployment)

Docker encapsulates all dependencies and is ideal for Linux servers with GPU.

#### 1. Install NVIDIA Container Toolkit (Linux only)

```bash
# Installation for Ubuntu
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-docker.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

#### 2. Build Images

```bash
cd RealtimeVoiceChat
docker compose build
```

> ⏱️ **Note**: The first build can take 15-30 minutes (downloads, model caching).

#### 3. Start Containers

```bash
docker compose up -d
```

#### 4. Load Ollama Model (once after start)

```bash
docker compose exec ollama ollama pull llama3
```

#### 5. Check Logs

```bash
# App logs
docker compose logs -f app

# Ollama logs
docker compose logs -f ollama
```

#### 6. Stop Containers

```bash
docker compose down
```

---

## Setting Up Ollama

Ollama is the default LLM backend. We recommend running it in WSL.

### Installation (WSL)

```bash
# In WSL (Ubuntu):
curl -fsSL https://ollama.com/install.sh | sh

# Download model:
ollama pull llama3
```

### Starting Ollama

```bash
# Start the Ollama server (in WSL):
ollama serve
```

Keep this terminal open while using the application.

### Check if Ollama is Running

```bash
# Should work without errors:
ollama list

# If "address already in use", Ollama is already running in the background
```

### Environment Variables (optional)

```bash
# If Ollama runs on different port/host:
export OLLAMA_BASE_URL="http://127.0.0.1:11434"

# Use different model:
export DEFAULT_LLM_MODEL="llama3"
```

---

## Starting the Server

> ⚠️ **Before starting:** Make sure Ollama is running first! If not using Docker, start Ollama in a separate WSL terminal:
> ```bash
> ollama serve
> ```

### With Conda

```powershell
# Activate environment (if not already active)
conda activate realtime-voice

# Navigate to code directory
cd RealtimeVoiceChat/code

# Start server
python server.py
```

The server will then run on `http://localhost:8000`.

### With Docker

```bash
# If not already started:
docker compose up -d

# Server runs automatically on port 8000
```

### Testing the Web Interface

1. Open browser: `http://localhost:8000`
2. Grant microphone permission
3. Click "Start" and speak

---

## Character Configuration

Characters are defined in `RealtimeVoiceChat/code/character_config.json`:

```json
{
  "LisaParker": {
    "display_name": "Lisa Parker",
    "tts_engine": "kokoro",
    "voice": "af_heart",
    "reference_audio": "reference_audio.wav",
    "system_prompt": "You are Lisa Parker...",
    "persist_history": false,
    "history": []
  },
  "PaulAdams": {
    "display_name": "Paul Adams",
    "tts_engine": "kokoro",
    "voice": "am_fenrir",
    "system_prompt": "You are Paul Adams...",
    "persist_history": false,
    "history": []
  }
}
```

### Configuration Options

| Field | Description | Example |
|-------|-------------|---------|
| `display_name` | Display name | `"Lisa Parker"` |
| `tts_engine` | TTS engine | `"kokoro"`, `"coqui"`, `"orpheus"` |
| `voice` | Voice ID | `"af_heart"` (Kokoro), `"v2/de_DE/..."` (Coqui) |
| `reference_audio` | Audio for voice cloning | `"my_voice.wav"` |
| `system_prompt` | Character personality | Any text |
| `llm_provider` | LLM backend (optional) | `"ollama"`, `"openai"` |
| `llm_model` | Model name (optional) | `"llama3"`, `"gpt-4"` |
| `persist_history` | Save history | `true`/`false` |
| `history` | Preloaded history | `[{"role": "user", "content": "..."}]` |

### TTS Engine Voices

**Kokoro** (fast, good quality):
- `af_heart`, `af_bella`, `af_sarah` (female)
- `am_adam`, `am_fenrir`, `am_michael` (male)

**Coqui** (best quality, slower):
- Supports voice cloning with `reference_audio`

**Orpheus** (experimental):
- Requires separate model

---

## 🎮 Game Manager LLM

The Game Manager is an AI "game master" that runs in the background and orchestrates the story.

### Features

- **Periodic Analysis**: Runs every X seconds (configurable, default: 30s)
- **Conversation Access**: Has access to all NPC conversation histories
- **Dynamic Injections**: Can inject instructions into any character
- **Game Clues**: Accepts hints from the game (e.g., "Player found evidence")
- **Web UI Panel**: Real-time status, timer, and controls

### Configuration (`game_manager_config.json`)

```json
{
  "enabled": true,
  "tick_interval_seconds": 30,
  "llm_provider": "ollama",
  "llm_model": "llama3",
  "story_context": "This is an interactive mystery. The player is a detective.",
  "known_characters": ["LisaParker", "PaulAdams"],
  "system_prompt": "You are the invisible game master of an interactive mystery..."
}
```

### Configuration Options

| Field | Description | Default |
|-------|-------------|---------|
| `enabled` | Enable/disable Game Manager | `true` |
| `tick_interval_seconds` | Time between analysis cycles | `30` |
| `llm_provider` | LLM backend for GM | `"ollama"` |
| `llm_model` | Model for GM | `"llama3"` |
| `story_context` | Background story info | `""` |
| `known_characters` | List of character IDs | `[]` |
| `system_prompt` | Instructions for the GM | `"..."` |

### How It Works

1. **Every X seconds** the Game Manager executes:
   - Collects all NPC conversation histories
   - Checks for new game clues
   - Analyzes the situation
   - Decides if characters need new instructions

2. **Output Format**:
   ```
   THINKING: [Analysis of the situation]
   ACTION: INJECT LisaParker: Become more nervous when the murder weapon is mentioned.
   ```

3. **Injections** are added to the character's system prompt:
   ```
   [GAME]: Become more nervous when the murder weapon is mentioned.
   ```

### Web UI

The Game Manager Panel in the web interface shows:
- **Status**: Active / Processing / Disabled
- **Timer**: Countdown until next tick
- **Trigger Button**: Manually trigger a tick
- **Game Clues**: Enter hints for the GM to consider
- **Last Thinking**: GM's last analysis
- **Last Actions**: Last injections
- **History**: History of all decisions

---

## 💉 System Prompt Injection

Inject context or instructions into characters during gameplay.

### Two Methods

#### 1. Direct Character Injection (Web UI)

Use the Inject field at the bottom of the chat to inject directly into the currently selected character.

**Example**: "The player just found a bloody weapon in the kitchen."

#### 2. Game Manager Clues (Web UI)

Add hints in the Game Manager Panel. The Game Manager analyzes these and decides itself which characters receive which instructions.

**Example**: "Player checked Paul's alibi - it doesn't match"

### Programmatic Injection

Via WebSocket message to a character:
```json
{
  "type": "inject",
  "content": "The player just found a bloody weapon."
}
```

Via WebSocket to Game Manager:
```json
{
  "type": "inject_clue",
  "content": "Player found evidence item #3"
}
```

### Injection in System Prompt

Injections are added to the character's system prompt:

```
[Original system prompt]

[GAME]: The player just found a bloody weapon.
[GAME]: Become more nervous when asked about the crime scene.
```

---

## ⚡ Performance: Global Generation Lock

With multiple "warm" NPC sessions, a global lock prevents LLM+TTS from running simultaneously for multiple characters. This:

- **Prevents GPU overload** with multiple parallel generations
- **Enables instant switching** between NPCs (< 1 second)
- **Serializes** heavy compute tasks automatically

The lock is managed transparently - from the player's perspective, it feels like instant switching between NPCs.

---

## Unity Integration

### Prerequisites

1. **Install NativeWebSocket Package**:
   - Open Unity Package Manager
   - Select "Add package from git URL..."
   - Enter URL: `https://github.com/endel/NativeWebSocket.git#upm`

2. **Copy Scripts**:
   - All `.cs` files from `RealtimeVoiceChatUnity/Assets/Scripts/` 
   - Copy to your Unity project under `Assets/Scripts/`

### Script Overview

#### `LiveLlmManager.cs` – Singleton for Microphone Management

**Purpose**: Central microphone recording, distributes audio to all active characters.

**Important Settings**:
```csharp
[SerializeField] private int sampleRate = 48_000;      // Must match server!
[SerializeField] private int chunkSamples = 2_048;     // Samples per chunk
[SerializeField] private string microphoneDeviceName;  // Empty = default microphone
```

**How It Works**:
- Automatically starts microphone recording when a character registers
- Automatically stops when no character is active
- Sends audio chunks to all registered `LiveLlmCharacterBase` instances

---

#### `LiveLlmCharacter.cs` (actually `LiveLlmCharacterBase`) – Character Base Class

**Purpose**: WebSocket connection, TTS playback, trigger-based activation.

**Important Settings**:
```csharp
[SerializeField] protected string characterId = "Character";  // Must match server config!
[SerializeField] private string wsUrl = "ws://127.0.0.1:8000/ws";
public AudioSource ttsSource;   // For TTS playback (automatically created)
public AudioSource monitorSource; // Optional: Microphone monitoring
public Transform player;        // Reference to player (for trigger)
```

**Automatic Behavior**:
- Connects automatically at `Start()`
- Activates conversation when player enters trigger area
- Deactivates conversation when player leaves the area

**Important Methods**:
```csharp
StartConversation()   // Manually start conversation
StopConversation()    // Manually end conversation
```

---

#### `ExampleLiveCharacter.cs` – Example Implementation

Shows how to create a concrete character:

```csharp
using UnityEngine;

public class Example_LiveLLM : LiveLlmCharacterBase
{
    protected override void Awake()
    {
        characterId = "PaulAdams";  // Must exist in character_config.json!
        base.Awake();
    }
}
```

### Setup in Unity

#### 1. Create Manager GameObject

1. Create empty GameObject: `GameObject > Create Empty`
2. Rename to "LiveLlmManager"
3. Add `LiveLlmManager.cs`
4. **Important**: This GameObject automatically becomes `DontDestroyOnLoad`

#### 2. Create Character NPC

1. Place 3D model in the scene
2. Create your own script (see example below)
3. Add script to NPC
4. **SphereCollider** is automatically added (trigger area)
5. Adjust collider radius (conversation range)
6. **AudioSource** for TTS is automatically created

#### 3. Configure Player

1. Player GameObject must have tag `"Player"`
2. Player needs a collider for trigger detection

### Example: Creating Your Own Character

```csharp
using UnityEngine;

public class MyCustomCharacter : LiveLlmCharacterBase
{
    [Header("Custom Settings")]
    [SerializeField] private Animator animator;
    
    protected override void Awake()
    {
        // Character ID must match server configuration
        characterId = "LisaParker";
        base.Awake();
    }
    
    protected override void OnTriggerEnter(Collider other)
    {
        base.OnTriggerEnter(other);
        
        // Custom logic when entering conversation area
        if (other.CompareTag("Player"))
        {
            animator?.SetBool("IsTalking", true);
        }
    }
    
    protected override void OnTriggerExit(Collider other)
    {
        base.OnTriggerExit(other);
        
        // Custom logic when leaving
        if (other.CompareTag("Player"))
        {
            animator?.SetBool("IsTalking", false);
        }
    }
}
```

### Unity Project Settings

1. **Player Settings > Other Settings**:
   - Enable `Run In Background` (important for WebSocket!)

2. **Audio Settings**:
   - Set sample rate to 48000 Hz (must match server)

---

## Data Flow & Protocol

### WebSocket Connection

**URL Format**: `ws://[host]:8000/ws?characterId=[CharacterID]`

Example: `ws://127.0.0.1:8000/ws?characterId=LisaParker`

### Binary Messages (Client → Server)

Audio chunks are sent as binary data:

```
┌─────────────┬─────────────┬──────────────────────┐
│ Timestamp   │ Flags       │ PCM Audio Data       │
│ (4 Bytes)   │ (4 Bytes)   │ (N Bytes)            │
│ Big-Endian  │ Big-Endian  │ 16-bit Signed LE     │
└─────────────┴─────────────┴──────────────────────┘
```

**Flags**:
- Bit 0: `isTTSPlaying` (1 = TTS is currently playing)

### JSON Messages

**Client → Server**:
```json
{"type": "tts_start", "character_id": "LisaParker"}
{"type": "tts_stop", "character_id": "LisaParker"}
{"type": "clear_history", "character_id": "LisaParker"}
{"type": "set_speed", "speed": 50, "character_id": "LisaParker"}
```

**Server → Client**:
```json
{"type": "partial_user_request", "content": "Hello, how...", "character_id": "LisaParker"}
{"type": "final_user_request", "content": "Hello, how are you?", "character_id": "LisaParker"}
{"type": "partial_assistant_answer", "content": "I'm doing...", "character_id": "LisaParker"}
{"type": "final_assistant_answer", "content": "I'm doing well!", "character_id": "LisaParker"}
{"type": "tts_chunk", "content": "[Base64-encoded PCM]", "character_id": "LisaParker"}
{"type": "tts_interruption", "character_id": "LisaParker"}
{"type": "stop_tts", "character_id": "LisaParker"}
```

---

## Troubleshooting

### "Ollama connection failed"

```
🤖💥 'ollama ps' command not found
```

**Solution**:
1. Install Ollama (see [Setting Up Ollama](#setting-up-ollama))
2. Check if Ollama is running: `ollama list`
3. If in WSL: Make sure the WSL service is running

### "TypeError: unsupported format string passed to NoneType"

```
TypeError: unsupported format string passed to NoneType.__format__
```

**Cause**: LLM initialization failed (often Ollama not reachable)

**Solution**: 
1. Start Ollama
2. Load model: `ollama pull llama3`
3. Restart server

### "IndentationError" on Server Start

**Cause**: Syntax error in Python files

**Solution**: Check that all files are correctly formatted (no tabs/spaces mix)

### Unity: WebSocket Not Connecting

**Check**:
1. Server running on port 8000?
2. `wsUrl` in script correct? (`ws://127.0.0.1:8000/ws`)
3. `characterId` exists in `character_config.json`?
4. Firewall blocking port 8000?

### Unity: No Audio

**Check**:
1. AudioSource present and not muted?
2. Audio Listener in the scene?
3. TTS engine running without errors? (Check server logs)

### High Latency

**Optimizations**:
1. Faster Whisper model: `base.en` instead of `large`
2. Kokoro instead of Coqui as TTS engine
3. Use GPU (not CPU)
4. Optimize Ollama model (Q4 quantization)

---

## Project Structure

```
RealtimeVoice/
├── README.md                          # This documentation
│
├── RealtimeVoiceChat/                 # Python Backend
│   ├── code/
│   │   ├── server.py                  # FastAPI WebSocket Server
│   │   ├── speech_pipeline_manager.py # LLM + TTS Orchestration
│   │   ├── game_manager.py            # 🎮 Game Manager LLM (NEW)
│   │   ├── audio_in.py                # Audio input processing
│   │   ├── audio_module.py            # TTS engine abstraction
│   │   ├── llm_module.py              # LLM backend abstraction
│   │   ├── transcribe.py              # STT configuration
│   │   ├── turndetect.py              # Speech pause detection
│   │   ├── character_config.json      # Character definitions
│   │   ├── game_manager_config.json   # 🎮 Game Manager Config (NEW)
│   │   ├── system_prompt.txt          # Default system prompt
│   │   └── static/                    # Web interface
│   │       ├── index.html             # UI with Game Manager Panel
│   │       ├── app.js                 # Client logic + GM integration
│   │       └── ...
│   │
│   ├── requirements.txt               # Python dependencies
│   ├── Dockerfile                     # Container definition
│   ├── docker-compose.yml             # Multi-container setup
│   └── install.bat                    # Windows installer
│
└── RealtimeVoiceChatUnity/            # Unity Client
    └── Assets/
        └── Scripts/
            ├── LiveLlmManager.cs      # Microphone manager (Singleton)
            ├── LiveLlmCharacter.cs    # Character base class
            └── ExampleLiveCharacter.cs # Example character
```

---

## License

This project is under the MIT License. However, note the licenses of the components used:
- **Coqui TTS**: CPML (Coqui Public Model License)
- **Ollama Models**: Varies by model (e.g., Llama License)
- **RealtimeSTT/TTS**: MIT

---

## Additional Resources

- [RealtimeSTT Documentation](https://github.com/KoljaB/RealtimeSTT)
- [RealtimeTTS Documentation](https://github.com/KoljaB/RealtimeTTS)
- [Ollama Documentation](https://ollama.com/docs)
- [NativeWebSocket for Unity](https://github.com/endel/NativeWebSocket)

---

## Citation

If you use this project in academic work, publications, or research, please cite it as:

> **LLM-NPC-Agents: Real-Time Context-Aware AI Characters with Dynamic Story Orchestration**  
> Lennart Schiweck  
> VR-Lab, Reutlingen University, 2025

### BibTeX

```bibtex
@software{schiweck2025llmnpcagents,
  author       = {Schiweck, Lennart},
  title        = {LLM-NPC-Agents: Real-Time Context-Aware AI Characters with Dynamic Story Orchestration},
  year         = {2025},
  institution  = {VR-Lab, Reutlingen University},
  url          = {https://github.com/lschiweck/LLM-NPC-Agents},
  note         = {WebSocket-based backend for intelligent NPCs with Game Manager orchestration. Includes Unity client reference implementation.}
}
```

---

*Developed at the VR-Lab, Reutlingen University*
