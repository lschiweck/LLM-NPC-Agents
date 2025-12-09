# LLM NPCs – Echtzeit-Sprachkonversation mit KI

**Hinweis:** Dieses Projekt ist eine **stark veränderte Version** von [KoljaB/RealtimeVoiceChat](https://github.com/KoljaB/RealtimeVoiceChat).  
Der technische **Core (Audio-Streaming, STT, TTS, WebSocket-Struktur)** basiert auf diesem Projekt, der Backend-Teil wurde jedoch deutlich erweitert und speziell für **Multi-LLM-NPC-Interaktionen** angepasst.

**Natürliche Sprachkonversationen mit mehreren LLM-gestützten NPCs in Echtzeit.**

Im Gegensatz zum Originalprojekt fokussiert sich diese Variante darauf, **mehrere eigenständige NPC-Charaktere** parallel zu betreiben, die:

- eigene System-Prompts, Wissensstände und Rollen besitzen,
- in einer gemeinsamen Szene miteinander und mit dem User interagieren können,
- über den Unity-Client gezielt angesprochen, aktiviert oder kombiniert werden können (z. B. Dialoge zwischen NPCs, Moderation durch den Spieler, Rollenwechsel).

Damit eignet sich dieses Projekt besonders für **VR-/Game-Szenarien**, in denen nicht nur ein einzelner Assistent, sondern ein ganzes **Ensemble von LLM-NPCs** dynamisch und sprachbasiert gesteuert werden soll.

Dieses Projekt ermöglicht es, per Sprache mit KI-Charakteren zu kommunizieren. Die Antworten werden in nahezu Echtzeit als synthetisierte Sprache zurückgegeben. Es besteht aus zwei Hauptkomponenten:

1. **Backend (Python/FastAPI)** – Speech-to-Text, LLM-Inferenz, Text-to-Speech
2. **Unity-Client (C#)** – Integration in VR/3D-Anwendungen

---

## Inhaltsverzeichnis

- [Architektur-Überblick](#architektur-überblick)
- [Systemanforderungen](#systemanforderungen)
- [Installation](#installation)
  - [Option A: Conda-Umgebung (empfohlen für Entwicklung)](#option-a-conda-umgebung-empfohlen-für-entwicklung)
  - [Option B: Docker (empfohlen für Deployment)](#option-b-docker-empfohlen-für-deployment)
- [Ollama einrichten](#ollama-einrichten)
- [Server starten](#server-starten)
- [Charakter-Konfiguration](#charakter-konfiguration)
- [Unity-Integration](#unity-integration)
  - [Voraussetzungen](#voraussetzungen)
  - [Skript-Übersicht](#skript-übersicht)
  - [Einrichtung in Unity](#einrichtung-in-unity)
  - [Beispiel: Eigenen Charakter erstellen](#beispiel-eigenen-charakter-erstellen)
- [Datenfluss & Protokoll](#datenfluss--protokoll)
- [Fehlerbehebung](#fehlerbehebung)
- [Projektstruktur](#projektstruktur)

---

## Architektur-Überblick

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              UNITY CLIENT                                    │
│  ┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────────┐  │
│  │ LiveLlmManager  │───▶│ LiveLlmCharacter │───▶│ AudioSource (Playback)  │  │
│  │ (Mikrofon)      │    │ (WebSocket)      │    │                         │  │
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

### Ablauf einer Konversation

1. **Sprachaufnahme**: Unity nimmt Mikrofon-Audio auf (48kHz, Mono, 16-bit PCM)
2. **Streaming**: Audio-Chunks werden via WebSocket an den Server gesendet
3. **Speech-to-Text**: `RealtimeSTT` wandelt Sprache in Text um (Whisper-basiert)
4. **LLM-Inferenz**: Der Text wird an Ollama/OpenAI gesendet, Antwort wird gestreamt
5. **Text-to-Speech**: `RealtimeTTS` synthetisiert die Antwort (Kokoro/Coqui/Orpheus)
6. **Audio-Streaming**: TTS-Chunks werden als Base64-kodiertes PCM zurückgesendet
7. **Wiedergabe**: Unity dekodiert und spielt die Antwort ab

---

## Systemanforderungen

| Komponente | Anforderung |
|------------|-------------|
| **Betriebssystem** | Windows 10/11, Linux (Ubuntu 22.04+) |
| **Python** | 3.10 oder 3.11 (nicht 3.12+) |
| **GPU** | NVIDIA mit CUDA 12.1+ (empfohlen, mindestens 8GB VRAM) |
| **RAM** | Mindestens 16GB |
| **Unity** | 2021.3 LTS oder neuer |

> ⚠️ **Wichtig**: Ohne NVIDIA-GPU ist die Performance stark eingeschränkt. STT und TTS laufen dann auf der CPU, was zu spürbarer Latenz führt.

---

## Installation

### Option A: Conda-Umgebung (empfohlen für Entwicklung)

#### 1. Conda-Umgebung erstellen

```powershell
# Neue Umgebung mit Python 3.10 erstellen
conda create -n realtime-voice python=3.10 -y

# Umgebung aktivieren
conda activate realtime-voice
```

#### 2. In das Code-Verzeichnis wechseln

```powershell
cd RealtimeVoiceChat/code
```

#### 3. PyTorch installieren (GPU-Version)

```powershell
# Für NVIDIA GPU mit CUDA 12.1
pip install torch==2.5.1+cu121 torchaudio==2.5.1+cu121 torchvision --index-url https://download.pytorch.org/whl/cu121
```

Für andere CUDA-Versionen siehe: https://pytorch.org/get-started/previous-versions/

#### 4. Abhängigkeiten installieren

```powershell
pip install -r requirements.txt
```

#### 5. Umgebung für spätere Nutzung

```powershell
# Zum erneuten Aktivieren:
conda activate realtime-voice
cd RealtimeVoiceChat/code
```

---

### Option B: Docker (empfohlen für Deployment)

Docker kapselt alle Abhängigkeiten und ist ideal für Linux-Server mit GPU.

#### 1. NVIDIA Container Toolkit installieren (nur Linux)

```bash
# Installation für Ubuntu
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | sudo apt-key add -
curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list | \
  sudo tee /etc/apt/sources.list.d/nvidia-docker.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

#### 2. Images bauen

```bash
cd RealtimeVoiceChat
docker compose build
```

> ⏱️ **Hinweis**: Der erste Build kann 15-30 Minuten dauern (Downloads, Model-Caching).

#### 3. Container starten

```bash
docker compose up -d
```

#### 4. Ollama-Modell laden (einmalig nach Start)

```bash
docker compose exec ollama ollama pull llama3
```

#### 5. Logs prüfen

```bash
# App-Logs
docker compose logs -f app

# Ollama-Logs
docker compose logs -f ollama
```

#### 6. Container stoppen

```bash
docker compose down
```

---

## Ollama einrichten

Ollama ist der Standard-LLM-Backend. Es kann lokal oder in WSL laufen.

### Installation (Windows mit WSL)

```bash
# In WSL (Ubuntu):
curl -fsSL https://ollama.com/install.sh | sh

# Modell herunterladen:
ollama pull llama3
```

### Installation (Windows nativ)

1. Installer von https://ollama.com/download herunterladen
2. Installieren und Terminal neu öffnen
3. `ollama pull llama3` ausführen

### Prüfen ob Ollama läuft

```bash
# Sollte ohne Fehler funktionieren:
ollama list

# Bei "address already in use" läuft Ollama bereits im Hintergrund
```

### Umgebungsvariablen (optional)

```powershell
# Falls Ollama auf anderem Port/Host läuft:
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# Anderes Modell verwenden:
$env:DEFAULT_LLM_MODEL = "llama3"
```

---

## Server starten

### Mit Conda

```powershell
# Umgebung aktivieren (falls noch nicht aktiv)
conda activate realtime-voice

# In Code-Verzeichnis wechseln
cd RealtimeVoiceChat/code

# Server starten
python server.py
```

Der Server läuft dann auf `http://localhost:8000`.

### Mit Docker

```bash
# Falls noch nicht gestartet:
docker compose up -d

# Server läuft automatisch auf Port 8000
```

### Web-Interface testen

1. Browser öffnen: `http://localhost:8000`
2. Mikrofon-Berechtigung erteilen
3. "Start" klicken und sprechen

---

## Charakter-Konfiguration

Charaktere werden in `RealtimeVoiceChat/code/character_config.json` definiert:

```json
{
  "LisaParker": {
    "display_name": "Lisa Parker",
    "tts_engine": "kokoro",
    "voice": "af_heart",
    "reference_audio": "reference_audio.wav",
    "system_prompt": "Du bist Lisa Parker...",
    "persist_history": false,
    "history": []
  },
  "PaulAdams": {
    "display_name": "Paul Adams",
    "tts_engine": "kokoro",
    "voice": "am_fenrir",
    "system_prompt": "Du bist Paul Adams...",
    "persist_history": false,
    "history": []
  }
}
```

### Konfigurations-Optionen

| Feld | Beschreibung | Beispiel |
|------|--------------|----------|
| `display_name` | Anzeigename | `"Lisa Parker"` |
| `tts_engine` | TTS-Engine | `"kokoro"`, `"coqui"`, `"orpheus"` |
| `voice` | Stimmen-ID | `"af_heart"` (Kokoro), `"v2/de_DE/..."` (Coqui) |
| `reference_audio` | Audio für Voice-Cloning | `"meine_stimme.wav"` |
| `system_prompt` | Charakter-Persönlichkeit | Beliebiger Text |
| `llm_provider` | LLM-Backend (optional) | `"ollama"`, `"openai"` |
| `llm_model` | Modellname (optional) | `"llama3"`, `"gpt-4"` |
| `persist_history` | Historie speichern | `true`/`false` |
| `history` | Vorgeladene Historie | `[{"role": "user", "content": "..."}]` |

### TTS-Engine-Stimmen

**Kokoro** (schnell, gute Qualität):
- `af_heart`, `af_bella`, `af_sarah` (weiblich)
- `am_adam`, `am_fenrir`, `am_michael` (männlich)

**Coqui** (beste Qualität, langsamer):
- Unterstützt Voice-Cloning mit `reference_audio`

**Orpheus** (experimentell):
- Benötigt separates Modell

---

## Unity-Integration

### Voraussetzungen

1. **NativeWebSocket-Package** installieren:
   - Unity Package Manager öffnen
   - "Add package from git URL..." wählen
   - URL eingeben: `https://github.com/endel/NativeWebSocket.git#upm`

2. **Skripte kopieren**:
   - Alle `.cs`-Dateien aus `RealtimeVoiceChatUnity/Assets/Scripts/` 
   - In dein Unity-Projekt unter `Assets/Scripts/` kopieren

### Skript-Übersicht

#### `LiveLlmManager.cs` – Singleton für Mikrofon-Verwaltung

**Aufgabe**: Zentrale Mikrofon-Aufnahme, verteilt Audio an alle aktiven Charaktere.

**Wichtige Einstellungen**:
```csharp
[SerializeField] private int sampleRate = 48_000;      // Muss mit Server übereinstimmen!
[SerializeField] private int chunkSamples = 2_048;     // Samples pro Chunk
[SerializeField] private string microphoneDeviceName;  // Leer = Standard-Mikrofon
```

**Funktionsweise**:
- Startet automatisch Mikrofon-Aufnahme wenn ein Charakter registriert wird
- Stoppt automatisch wenn kein Charakter mehr aktiv ist
- Sendet Audio-Chunks an alle registrierten `LiveLlmCharacterBase`-Instanzen

---

#### `LiveLlmCharacter.cs` (eigentlich `LiveLlmCharacterBase`) – Charakter-Basisklasse

**Aufgabe**: WebSocket-Verbindung, TTS-Wiedergabe, Trigger-basierte Aktivierung.

**Wichtige Einstellungen**:
```csharp
[SerializeField] protected string characterId = "Character";  // Muss mit Server-Config übereinstimmen!
[SerializeField] private string wsUrl = "ws://127.0.0.1:8000/ws";
public AudioSource ttsSource;   // Für TTS-Wiedergabe (wird automatisch erstellt)
public AudioSource monitorSource; // Optional: Mikrofon-Monitoring
public Transform player;        // Referenz zum Spieler (für Trigger)
```

**Automatisches Verhalten**:
- Verbindet automatisch bei `Start()`
- Aktiviert Konversation wenn Spieler den Trigger-Bereich betritt
- Deaktiviert Konversation wenn Spieler den Bereich verlässt

**Wichtige Methoden**:
```csharp
StartConversation()   // Manuell Konversation starten
StopConversation()    // Manuell Konversation beenden
```

---

#### `ExampleLiveCharacter.cs` – Beispiel-Implementation

Zeigt, wie man einen konkreten Charakter erstellt:

```csharp
using UnityEngine;

public class Example_LiveLLM : LiveLlmCharacterBase
{
    protected override void Awake()
    {
        characterId = "PaulAdams";  // Muss in character_config.json existieren!
        base.Awake();
    }
}
```

### Einrichtung in Unity

#### 1. Manager-GameObject erstellen

1. Leeres GameObject erstellen: `GameObject > Create Empty`
2. Umbenennen zu "LiveLlmManager"
3. `LiveLlmManager.cs` hinzufügen
4. **Wichtig**: Dieses GameObject wird automatisch `DontDestroyOnLoad`

#### 2. Charakter-NPC erstellen

1. 3D-Modell in die Szene platzieren
2. Eigenes Skript erstellen (siehe Beispiel unten)
3. Skript zum NPC hinzufügen
4. **SphereCollider** wird automatisch hinzugefügt (Trigger-Bereich)
5. Collider-Radius anpassen (Gesprächsreichweite)
6. **AudioSource** für TTS wird automatisch erstellt

#### 3. Spieler konfigurieren

1. Spieler-GameObject muss Tag `"Player"` haben
2. Spieler braucht einen Collider für Trigger-Erkennung

### Beispiel: Eigenen Charakter erstellen

```csharp
using UnityEngine;

public class MyCustomCharacter : LiveLlmCharacterBase
{
    [Header("Custom Settings")]
    [SerializeField] private Animator animator;
    
    protected override void Awake()
    {
        // Character-ID muss mit Server-Konfiguration übereinstimmen
        characterId = "LisaParker";
        base.Awake();
    }
    
    protected override void OnTriggerEnter(Collider other)
    {
        base.OnTriggerEnter(other);
        
        // Eigene Logik beim Betreten des Gesprächsbereichs
        if (other.CompareTag("Player"))
        {
            animator?.SetBool("IsTalking", true);
        }
    }
    
    protected override void OnTriggerExit(Collider other)
    {
        base.OnTriggerExit(other);
        
        // Eigene Logik beim Verlassen
        if (other.CompareTag("Player"))
        {
            animator?.SetBool("IsTalking", false);
        }
    }
}
```

### Unity-Projekt-Einstellungen

1. **Player Settings > Other Settings**:
   - `Run In Background` aktivieren (wichtig für WebSocket!)

2. **Audio Settings**:
   - Sample Rate auf 48000 Hz setzen (muss mit Server übereinstimmen)

---

## Datenfluss & Protokoll

### WebSocket-Verbindung

**URL-Format**: `ws://[host]:8000/ws?characterId=[CharacterID]`

Beispiel: `ws://127.0.0.1:8000/ws?characterId=LisaParker`

### Binär-Nachrichten (Client → Server)

Audio-Chunks werden als Binärdaten gesendet:

```
┌─────────────┬─────────────┬──────────────────────┐
│ Timestamp   │ Flags       │ PCM Audio Data       │
│ (4 Bytes)   │ (4 Bytes)   │ (N Bytes)            │
│ Big-Endian  │ Big-Endian  │ 16-bit Signed LE     │
└─────────────┴─────────────┴──────────────────────┘
```

**Flags**:
- Bit 0: `isTTSPlaying` (1 = TTS spielt gerade ab)

### JSON-Nachrichten

**Client → Server**:
```json
{"type": "tts_start", "character_id": "LisaParker"}
{"type": "tts_stop", "character_id": "LisaParker"}
{"type": "clear_history", "character_id": "LisaParker"}
{"type": "set_speed", "speed": 50, "character_id": "LisaParker"}
```

**Server → Client**:
```json
{"type": "partial_user_request", "content": "Hallo, wie...", "character_id": "LisaParker"}
{"type": "final_user_request", "content": "Hallo, wie geht es dir?", "character_id": "LisaParker"}
{"type": "partial_assistant_answer", "content": "Mir geht es...", "character_id": "LisaParker"}
{"type": "final_assistant_answer", "content": "Mir geht es gut!", "character_id": "LisaParker"}
{"type": "tts_chunk", "content": "[Base64-kodiertes PCM]", "character_id": "LisaParker"}
{"type": "tts_interruption", "character_id": "LisaParker"}
{"type": "stop_tts", "character_id": "LisaParker"}
```

---

## Fehlerbehebung

### "Ollama connection failed"

```
🤖💥 'ollama ps' command not found
```

**Lösung**:
1. Ollama installieren (siehe [Ollama einrichten](#ollama-einrichten))
2. Prüfen ob Ollama läuft: `ollama list`
3. Falls in WSL: Sicherstellen dass der WSL-Service läuft

### "TypeError: unsupported format string passed to NoneType"

```
TypeError: unsupported format string passed to NoneType.__format__
```

**Ursache**: LLM-Initialisierung fehlgeschlagen (oft Ollama nicht erreichbar)

**Lösung**: 
1. Ollama starten
2. Modell laden: `ollama pull llama3`
3. Server neu starten

### "IndentationError" beim Server-Start

**Ursache**: Syntaxfehler in Python-Dateien

**Lösung**: Prüfen ob alle Dateien korrekt formatiert sind (keine Tabs/Spaces-Mischung)

### Unity: WebSocket verbindet nicht

**Prüfen**:
1. Server läuft auf Port 8000?
2. `wsUrl` im Skript korrekt? (`ws://127.0.0.1:8000/ws`)
3. `characterId` existiert in `character_config.json`?
4. Firewall blockiert Port 8000?

### Unity: Kein Audio

**Prüfen**:
1. AudioSource vorhanden und nicht stumm?
2. Audio-Listener in der Szene?
3. TTS-Engine läuft fehlerfrei? (Server-Logs prüfen)

### Hohe Latenz

**Optimierungen**:
1. Schnelleres Whisper-Modell: `base.en` statt `large`
2. Kokoro statt Coqui als TTS-Engine
3. GPU verwenden (nicht CPU)
4. Ollama-Modell optimieren (Q4-Quantisierung)

---

## Projektstruktur

```
RealtimeVoice/
├── README.md                          # Diese Dokumentation
│
├── RealtimeVoiceChat/                 # Python Backend
│   ├── code/
│   │   ├── server.py                  # FastAPI WebSocket Server
│   │   ├── speech_pipeline_manager.py # LLM + TTS Orchestrierung
│   │   ├── audio_in.py                # Audio-Eingabe-Verarbeitung
│   │   ├── audio_module.py            # TTS-Engine-Abstraktion
│   │   ├── llm_module.py              # LLM-Backend-Abstraktion
│   │   ├── transcribe.py              # STT-Konfiguration
│   │   ├── turndetect.py              # Sprechpausen-Erkennung
│   │   ├── character_config.json      # Charakter-Definitionen
│   │   ├── system_prompt.txt          # Standard-System-Prompt
│   │   └── static/                    # Web-Interface
│   │       ├── index.html
│   │       ├── app.js
│   │       └── ...
│   │
│   ├── requirements.txt               # Python-Abhängigkeiten
│   ├── Dockerfile                     # Container-Definition
│   ├── docker-compose.yml             # Multi-Container-Setup
│   └── install.bat                    # Windows-Installer
│
└── RealtimeVoiceChatUnity/            # Unity Client
    └── Assets/
        └── Scripts/
            ├── LiveLlmManager.cs      # Mikrofon-Manager (Singleton)
            ├── LiveLlmCharacter.cs    # Charakter-Basisklasse
            └── ExampleLiveCharacter.cs # Beispiel-Charakter
```

---

## Lizenz

Das Projekt steht unter der MIT-Lizenz. Beachte jedoch die Lizenzen der verwendeten Komponenten:
- **Coqui TTS**: CPML (Coqui Public Model License)
- **Ollama-Modelle**: Variiert je nach Modell (z.B. Llama License)
- **RealtimeSTT/TTS**: MIT

---

## Weitere Ressourcen

- [RealtimeSTT Dokumentation](https://github.com/KoljaB/RealtimeSTT)
- [RealtimeTTS Dokumentation](https://github.com/KoljaB/RealtimeTTS)
- [Ollama Dokumentation](https://ollama.com/docs)
- [NativeWebSocket für Unity](https://github.com/endel/NativeWebSocket)

