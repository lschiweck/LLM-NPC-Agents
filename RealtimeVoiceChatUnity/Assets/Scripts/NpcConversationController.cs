using System;
using System.Collections;
using System.Text;
using System.Threading.Tasks;
using NativeWebSocket;
using UnityEngine;
using UnityEngine.Events;

/// <summary>
/// Controls NPC-to-NPC conversations.
/// Routes audio from server to the correct character's AudioSource.
/// Can trigger conversations manually or on a timer (for testing).
/// </summary>
public class NpcConversationController : MonoBehaviour
{
    public static NpcConversationController Instance { get; private set; }

    [Header("WebSocket")]
    [SerializeField] private string wsUrl = "ws://127.0.0.1:8000/ws/npc_conversation";

    [Header("Auto-Trigger (Testing/Legacy)")]
    [Tooltip("Enable automatic conversations on a timer. Disable when using NpcInjectionTriggerSystem.")]
    [SerializeField] private bool autoTriggerEnabled = false;
    [Tooltip("Seconds between auto-triggered conversations")]
    [SerializeField] private float autoTriggerInterval = 15f;
    [Tooltip("Number of turns per auto-triggered conversation")]
    [SerializeField] private int autoTriggerTurns = 3;
    [Tooltip("Character IDs for auto-trigger (leave empty to use first two registered)")]
    [SerializeField] private string[] autoTriggerCharacters = new string[] { "LisaParker", "PaulAdams" };
    [Tooltip("Context/topic for auto-triggered conversations")]
    [SerializeField] private string autoTriggerContext = "Have a brief casual conversation.";

    [Header("Events")]
    public UnityEvent<string, string> OnConversationTurn; // speaker, message
    public UnityEvent<string> OnConversationStateChanged; // state
    public UnityEvent OnConversationStarted;
    public UnityEvent OnConversationEnded;
    public UnityEvent OnConversationInterrupted; // Player started speaking

    private WebSocket ws;
    private bool isConversationRunning;
    private Coroutine autoTriggerCoroutine;
    
    // Audio queue to prevent speakers from overlapping
    private System.Collections.Generic.Queue<(string speakerId, string base64Audio)> audioQueue = new();
    private bool isPlayingAudio = false;
    private Coroutine audioPlaybackCoroutine;
    
    // Interruption handling
    private bool isInterrupted = false;
    private string currentPlayingSpeakerId = null;
    
    // Track conversation participants for look-at system
    private string currentNpc1Id = null;
    private string currentNpc2Id = null;

    [Serializable]
    private class ServerMessage
    {
        public string type;
        public string speaker_id;  // Used in npc_conversation_turn
        public string message;
        public int turn_number;
        public bool is_last;
        public string state;       // Used in npc_conversation_state
        public int current_turn;
        public int max_turns;
    }

    [Serializable]
    private class StartConversationMessage
    {
        public string type = "start_conversation";
        public string npc1_id;
        public string npc2_id;
        public int turns;
        public string context;
    }

    [Serializable]
    private class StopConversationMessage
    {
        public string type = "stop_conversation";
    }

    private void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Destroy(gameObject);
            return;
        }
        Instance = this;
        DontDestroyOnLoad(gameObject);
    }

    private void Start()
    {
        Debug.Log($"[NpcConversation] Start() called. AutoTriggerEnabled: {autoTriggerEnabled}");
        
        // Start auto-trigger FIRST (before WebSocket blocks)
        if (autoTriggerEnabled)
        {
            Debug.Log("[NpcConversation] Starting auto-trigger coroutine...");
            autoTriggerCoroutine = StartCoroutine(AutoTriggerLoop());
        }
        else
        {
            Debug.Log("[NpcConversation] Auto-trigger is DISABLED in inspector.");
        }
        
        // Connect WebSocket (don't await - let it run in background)
        _ = ConnectWebSocket();
    }

    private void Update()
    {
#if !UNITY_WEBGL || UNITY_EDITOR
        ws?.DispatchMessageQueue();
#endif
    }

    private void OnDestroy()
    {
        if (autoTriggerCoroutine != null)
        {
            StopCoroutine(autoTriggerCoroutine);
        }
        ws?.Close();
        ws = null;
    }

    private async Task ConnectWebSocket()
    {
        ws = new WebSocket(wsUrl);

        ws.OnOpen += () =>
        {
            Debug.Log("[NpcConversation] WebSocket connected.");
        };

        ws.OnError += e => Debug.LogError($"[NpcConversation] WebSocket error: {e}");
        ws.OnClose += e => Debug.Log($"[NpcConversation] WebSocket closed: {e}");
        ws.OnMessage += HandleServerMessage;

        try
        {
            await ws.Connect();
        }
        catch (Exception ex)
        {
            Debug.LogError($"[NpcConversation] WebSocket connect failed: {ex.Message}");
        }
    }

    private void HandleServerMessage(byte[] bytes)
    {
        if (bytes == null || bytes.Length == 0) return;

        // Check if this is binary audio data (starts with speaker ID header, 32 bytes)
        // Binary audio has a 32-byte header with speaker ID, then PCM audio data
        // JSON messages start with '{' (0x7B)
        if (bytes[0] != 0x7B) // Not a JSON message (doesn't start with '{')
        {
            HandleBinaryAudio(bytes);
            return;
        }

        string json;
        try
        {
            json = Encoding.UTF8.GetString(bytes);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[NpcConversation] Failed to decode message: {ex.Message}");
            return;
        }

        ServerMessage msg;
        try
        {
            msg = JsonUtility.FromJson<ServerMessage>(json);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[NpcConversation] Invalid JSON: {ex.Message}");
            return;
        }

        switch (msg.type)
        {
            case "npc_conversation_state":
                HandleStateUpdate(msg);
                break;

            case "npc_conversation_turn":
                HandleTurn(msg);
                break;

            case "available_characters":
                // Server sends list of available characters - can be used for UI
                Debug.Log($"[NpcConversation] Available characters received.");
                break;
            
            case "npc_conversation_interrupted":
                HandleInterruption();
                break;

            default:
                Debug.Log($"[NpcConversation] Unhandled message type: {msg.type}");
                break;
        }
    }

    private void HandleBinaryAudio(byte[] bytes)
    {
        // Binary format: 32-byte speaker ID header (null-padded) + PCM audio data
        if (bytes.Length <= 32)
        {
            Debug.LogWarning("[NpcConversation] Binary message too short for audio.");
            return;
        }

        // Extract speaker ID from first 32 bytes (null-terminated string)
        string speakerId = Encoding.UTF8.GetString(bytes, 0, 32).TrimEnd('\0');
        
        // Extract audio data (everything after header)
        int audioLength = bytes.Length - 32;
        byte[] audioBytes = new byte[audioLength];
        Array.Copy(bytes, 32, audioBytes, 0, audioLength);

        Debug.Log($"[NpcConversation] Received {audioLength} bytes of audio for {speakerId}");

        // Convert to base64 and queue for playback (to prevent overlap)
        string base64Audio = Convert.ToBase64String(audioBytes);
        audioQueue.Enqueue((speakerId, base64Audio));
        
        // Start playback coroutine if not running
        if (audioPlaybackCoroutine == null)
        {
            audioPlaybackCoroutine = StartCoroutine(ProcessAudioQueue());
        }
    }
    
    private IEnumerator ProcessAudioQueue()
    {
        const int NPC_SAMPLE_RATE = 24000; // TTS outputs at 24kHz
        
        while ((audioQueue.Count > 0 || isPlayingAudio) && !isInterrupted)
        {
            if (audioQueue.Count > 0 && !isPlayingAudio && !isInterrupted)
            {
                var (speakerId, base64Audio) = audioQueue.Dequeue();
                
                var character = LiveLlmCharacterBase.GetCharacter(speakerId);
                if (character != null)
                {
                    // Route through character's main ttsSource via PlayExternalTtsChunk
                    // This ensures OVR Lip Sync (which monitors ttsSource) works for NPC-NPC convos
                    Debug.Log($"[NpcConversation] Playing audio for {speakerId} via PlayExternalTtsChunk (lip sync compatible)");
                    isPlayingAudio = true;
                    currentPlayingSpeakerId = speakerId;

                    // Decode to get duration for waiting
                    byte[] audioBytes = Convert.FromBase64String(base64Audio);
                    int sampleCount = audioBytes.Length / 2;
                    if (sampleCount <= 0)
                    {
                        Debug.LogWarning($"[NpcConversation] Empty audio for {speakerId}");
                        isPlayingAudio = false;
                        currentPlayingSpeakerId = null;
                        continue;
                    }

                    // Play through character's main audio path (triggers lip sync)
                    character.PlayExternalTtsChunk(base64Audio);
                    
                    // Calculate duration and wait for playback to complete
                    // PlayExternalTtsChunk upsamples 24kHz to 48kHz, so duration stays the same
                    float durationSeconds = sampleCount / (float)NPC_SAMPLE_RATE;
                    float waitTime = 0f;
                    
                    while (waitTime < durationSeconds && !isInterrupted)
                    {
                        yield return new WaitForSeconds(0.05f);
                        waitTime += 0.05f;
                    }
                    
                    if (isInterrupted)
                    {
                        Debug.Log($"[NpcConversation] Playback interrupted for {speakerId}");
                        // Stop audio on the character
                        character.StopAllAudioImmediately();
                    }
                    
                    isPlayingAudio = false;
                    currentPlayingSpeakerId = null;
                }
                else
                {
                    Debug.LogWarning($"[NpcConversation] Character '{speakerId}' not found. Available: {string.Join(", ", LiveLlmCharacterBase.AllCharacters.Keys)}");
                }
            }
            else
            {
                yield return null;
            }
        }
        
        // Clear any remaining audio if interrupted
        if (isInterrupted)
        {
            audioQueue.Clear();
            Debug.Log("[NpcConversation] Cleared remaining audio queue due to interruption.");
            // Look targets already cleared in HandleInterruption
        }
        else
        {
            // Audio finished naturally - clear NPC look targets so they stop looking at each other
            ClearNpcLookTargets();
        }
        
        audioPlaybackCoroutine = null;
        currentPlayingSpeakerId = null;
        Debug.Log("[NpcConversation] Audio queue empty, playback coroutine finished.");
    }

    private void HandleStateUpdate(ServerMessage msg)
    {
        Debug.Log($"[NpcConversation] State: {msg.state} (turn {msg.current_turn}/{msg.max_turns})");
        
        OnConversationStateChanged?.Invoke(msg.state);

        if (msg.state == "running")
        {
            isConversationRunning = true;
            isInterrupted = false;  // Reset on new conversation
            
            // Make NPCs look at each other
            SetNpcLookTargets();
            
            OnConversationStarted?.Invoke();
        }
        else if (msg.state == "finished" || msg.state == "stopped" || msg.state == "stopping" || msg.state == "error")
        {
            isConversationRunning = false;
            
            // DON'T clear look targets here - audio may still be playing!
            // Look targets are cleared when ProcessAudioQueue finishes (audio done)
            // or when HandleInterruption is called (player interrupts)
            
            OnConversationEnded?.Invoke();
        }
    }

    private void HandleTurn(ServerMessage msg)
    {
        Debug.Log($"[NpcConversation] Turn {msg.turn_number} - {msg.speaker_id}: {msg.message}");
        OnConversationTurn?.Invoke(msg.speaker_id, msg.message);
    }

    /// <summary>
    /// Handle interruption message from server - player is speaking, stop NPC audio immediately.
    /// </summary>
    private void HandleInterruption()
    {
        Debug.Log("[NpcConversation] 🛑 INTERRUPT RECEIVED - stopping ALL NPC audio NOW");
        isInterrupted = true;
        
        // Stop the playback coroutine first
        if (audioPlaybackCoroutine != null)
        {
            StopCoroutine(audioPlaybackCoroutine);
            audioPlaybackCoroutine = null;
            Debug.Log("[NpcConversation] Stopped audio playback coroutine");
        }
        
        // Clear the audio queue immediately
        audioQueue.Clear();
        Debug.Log("[NpcConversation] Cleared audio queue");

        // Stop audio on the currently speaking character (now routed through ttsSource)
        if (!string.IsNullOrEmpty(currentPlayingSpeakerId))
        {
            var character = LiveLlmCharacterBase.GetCharacter(currentPlayingSpeakerId);
            if (character != null)
            {
                character.StopAllAudioImmediately();
            }
        }
        
        // Also stop all characters that might have buffered audio
        foreach (var kvp in LiveLlmCharacterBase.AllCharacters)
        {
            kvp.Value?.StopAllAudioImmediately();
        }
        
        // Clear look targets - NPCs stop looking at each other
        ClearNpcLookTargets();
        
        currentPlayingSpeakerId = null;
        isPlayingAudio = false;
        isConversationRunning = false;
        OnConversationInterrupted?.Invoke();
        OnConversationEnded?.Invoke();
        
        Debug.Log("[NpcConversation] ✅ Interruption complete - all audio stopped");
    }

    /// <summary>
    /// Start a conversation between two NPCs.
    /// </summary>
    /// <param name="npc1">First character ID (must match character_config.json)</param>
    /// <param name="npc2">Second character ID</param>
    /// <param name="turns">Number of back-and-forth exchanges</param>
    /// <param name="context">Topic or context for the conversation</param>
    public async void StartConversation(string npc1, string npc2, int turns = 3, string context = "")
    {
        if (ws == null || ws.State != WebSocketState.Open)
        {
            Debug.LogError("[NpcConversation] WebSocket not connected.");
            return;
        }

        if (isConversationRunning)
        {
            Debug.LogWarning("[NpcConversation] Conversation already running.");
            return;
        }
        
        // Reset interruption flag when starting a new conversation
        isInterrupted = false;
        
        // Store participant IDs for look-at system
        currentNpc1Id = npc1;
        currentNpc2Id = npc2;

        var msg = new StartConversationMessage
        {
            npc1_id = npc1,
            npc2_id = npc2,
            turns = turns,
            context = context
        };

        string json = JsonUtility.ToJson(msg);
        Debug.Log($"[NpcConversation] Starting: {npc1} <-> {npc2}, {turns} turns, context: '{context}'");

        try
        {
            await ws.SendText(json);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[NpcConversation] Failed to send start message: {ex.Message}");
        }
    }

    /// <summary>
    /// Stop the current NPC conversation.
    /// </summary>
    public async void StopConversation()
    {
        if (ws == null || ws.State != WebSocketState.Open)
        {
            Debug.LogError("[NpcConversation] WebSocket not connected.");
            return;
        }

        var msg = new StopConversationMessage();
        string json = JsonUtility.ToJson(msg);

        Debug.Log("[NpcConversation] Stopping conversation.");

        try
        {
            await ws.SendText(json);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[NpcConversation] Failed to send stop message: {ex.Message}");
        }
    }

    /// <summary>
    /// Check if a conversation is currently running.
    /// </summary>
    public bool IsConversationRunning => isConversationRunning;

    private IEnumerator AutoTriggerLoop()
    {
        // Wait for characters to register
        Debug.Log($"[NpcConversation] Auto-trigger enabled. Waiting 5s for characters, then {autoTriggerInterval}s between triggers.");
        yield return new WaitForSeconds(5f);

        while (autoTriggerEnabled)
        {
            Debug.Log($"[NpcConversation] Next auto-trigger in {autoTriggerInterval} seconds...");
            yield return new WaitForSeconds(autoTriggerInterval);

            if (isConversationRunning)
            {
                Debug.Log("[NpcConversation] Auto-trigger skipped: conversation already running.");
                continue;
            }

            // Determine which characters to use
            string npc1 = null, npc2 = null;

            if (autoTriggerCharacters != null && autoTriggerCharacters.Length >= 2)
            {
                npc1 = autoTriggerCharacters[0];
                npc2 = autoTriggerCharacters[1];
            }
            else
            {
                // Use first two registered characters
                var keys = new System.Collections.Generic.List<string>(LiveLlmCharacterBase.AllCharacters.Keys);
                if (keys.Count >= 2)
                {
                    npc1 = keys[0];
                    npc2 = keys[1];
                }
            }

            if (string.IsNullOrEmpty(npc1) || string.IsNullOrEmpty(npc2))
            {
                Debug.LogWarning("[NpcConversation] Auto-trigger: Not enough characters registered.");
                continue;
            }

            Debug.Log($"[NpcConversation] Auto-triggering conversation: {npc1} <-> {npc2}");
            StartConversation(npc1, npc2, autoTriggerTurns, autoTriggerContext);
        }
    }

    /// <summary>
    /// Enable or disable auto-trigger at runtime.
    /// </summary>
    public void SetAutoTrigger(bool enabled, float interval = 60f)
    {
        autoTriggerEnabled = enabled;
        autoTriggerInterval = interval;

        if (autoTriggerCoroutine != null)
        {
            StopCoroutine(autoTriggerCoroutine);
            autoTriggerCoroutine = null;
        }

        if (enabled)
        {
            autoTriggerCoroutine = StartCoroutine(AutoTriggerLoop());
        }
    }

    #region Look At System
    
    /// <summary>
    /// Make the conversation participants look at each other.
    /// </summary>
    private void SetNpcLookTargets()
    {
        if (string.IsNullOrEmpty(currentNpc1Id)) return;
        
        var npc1 = LiveLlmCharacterBase.GetCharacter(currentNpc1Id);
        var npc2 = !string.IsNullOrEmpty(currentNpc2Id) 
            ? LiveLlmCharacterBase.GetCharacter(currentNpc2Id) 
            : null;
        
        if (npc1 != null && npc2 != null)
        {
            // Two NPCs talking - make them look at each other
            npc1.SetLookTarget(npc2.transform);
            npc2.SetLookTarget(npc1.transform);
            Debug.Log($"[NpcConversation] 👀 {currentNpc1Id} and {currentNpc2Id} now looking at each other");
        }
        else if (npc1 != null)
        {
            // Monologue - NPC could look at player or just stay as is
            Debug.Log($"[NpcConversation] 👀 {currentNpc1Id} monologue - no look target set");
        }
    }
    
    /// <summary>
    /// Clear look targets for conversation participants.
    /// </summary>
    private void ClearNpcLookTargets()
    {
        if (!string.IsNullOrEmpty(currentNpc1Id))
        {
            var npc1 = LiveLlmCharacterBase.GetCharacter(currentNpc1Id);
            npc1?.ClearLookTarget();
        }
        
        if (!string.IsNullOrEmpty(currentNpc2Id))
        {
            var npc2 = LiveLlmCharacterBase.GetCharacter(currentNpc2Id);
            npc2?.ClearLookTarget();
        }
        
        Debug.Log("[NpcConversation] 👀 Cleared NPC look targets");
        
        currentNpc1Id = null;
        currentNpc2Id = null;
    }
    
    #endregion
}
