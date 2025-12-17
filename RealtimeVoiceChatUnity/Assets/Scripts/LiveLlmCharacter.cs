using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using System.Threading.Tasks;
using NativeWebSocket;
using UnityEngine;

[RequireComponent(typeof(SphereCollider))]
public class LiveLlmCharacterBase : MonoBehaviour
{
    // Static registry of all characters by ID (for NPC conversations)
    private static Dictionary<string, LiveLlmCharacterBase> characterRegistry = new();
    public static IReadOnlyDictionary<string, LiveLlmCharacterBase> AllCharacters => characterRegistry;

    public static LiveLlmCharacterBase GetCharacter(string id)
    {
        characterRegistry.TryGetValue(id, out var character);
        return character;
    }

    [Header("Character")]
    [SerializeField] protected string characterId = "Character";
    public string CharacterId => characterId;

    [Header("References")]
    public Transform player;

    [Header("Audio")]
    public AudioSource ttsSource;
    public AudioSource monitorSource;

    [Header("WebSocket")]
    [SerializeField] private string wsUrl = "ws://127.0.0.1:8000/ws";

    private const int SampleRate = 48_000;
    private const float TtsStopGrace = 1.5f;
    private const float TtsStopMinDelay = 2.0f;

    private WebSocket ws;
    private Coroutine ttsStopCoroutine;
    private bool isTtsPlayingClient;
    private bool isConversationActive;

    // TTS stream buffer
    private const int TtsBufferSeconds = 30;
    private readonly object ttsBufferLock = new();
    private float[] ttsBuffer = new float[SampleRate * TtsBufferSeconds];
    private int ttsWriteIndex;
    private int ttsReadIndex;
    private int ttsBufferedSamples;
    private AudioClip ttsStreamClip;

    [Serializable]
    private class ServerMessage
    {
        public string type;
        public string content;
        public string character_id;
    }

    [Serializable]
    private class ClientMessage
    {
        public string type;
        public string content;
        public string character_id;
    }

    protected virtual void Awake()
    {
        if (!ttsSource)
        {
            ttsSource = GetComponent<AudioSource>() ?? gameObject.AddComponent<AudioSource>();
        }

        ttsSource.playOnAwake = false;
        ttsSource.loop = true;

        ttsSource.spatialBlend = 1f;
        ttsSource.rolloffMode = AudioRolloffMode.Logarithmic;
        ttsSource.minDistance = 1.5f;
        ttsSource.maxDistance = 15f;
        ttsSource.dopplerLevel = 0f;

        if (monitorSource)
        {
            monitorSource.playOnAwake = false;
            monitorSource.loop = false;
            monitorSource.spatialBlend = 0f; 
        }

        SetupTtsStream();
    }

    private void SetupTtsStream()
    {
        if (ttsStreamClip != null) return;

        ttsStreamClip = AudioClip.Create(
            $"{characterId}_TtsStream",
            SampleRate * 2,
            1,
            SampleRate,
            true,
            OnAudioRead,
            OnSetAudioPosition
        );

        ttsSource.clip = ttsStreamClip;
        ttsSource.Play();
    }

    protected virtual async void Start()
    {
        Application.runInBackground = true;
        
        // Register in static registry for NPC conversations
        if (!string.IsNullOrEmpty(characterId))
        {
            characterRegistry[characterId] = this;
            Debug.Log($"[{characterId}] Registered in character registry.");
        }
        
        await SetupWebSocket();
    }

    private async Task SetupWebSocket()
    {
        string url = wsUrl.Contains("?")
            ? $"{wsUrl}&characterId={Uri.EscapeDataString(characterId)}"
            : $"{wsUrl}?characterId={Uri.EscapeDataString(characterId)}";

        ws = new WebSocket(url);

        ws.OnOpen += () =>
        {
            Debug.Log($"[{characterId}] WS connected.");
            ForceStopTts(false);
            if (isConversationActive)
            {
                LiveLlmManager.Instance?.RegisterCharacter(this);
            }
        };

        ws.OnError += e => Debug.LogError($"[{characterId}] WS error: {e}");
        ws.OnClose += e => Debug.Log($"[{characterId}] WS closed with code: {e}");
        ws.OnMessage += HandleServerMessage;

        try
        {
            await ws.Connect();
        }
        catch (Exception ex)
        {
            Debug.LogError($"[{characterId}] WS connect failed: {ex.Message}");
        }
    }

    public void DispatchWebsocket()
    {
#if !UNITY_WEBGL || UNITY_EDITOR
        ws?.DispatchMessageQueue();
#endif
    }

    public bool IsTtsPlayingClient => isTtsPlayingClient;

    protected virtual void OnDestroy()
    {
        // Unregister from static registry
        if (!string.IsNullOrEmpty(characterId) && characterRegistry.ContainsKey(characterId))
        {
            characterRegistry.Remove(characterId);
        }
        
        LiveLlmManager.Instance?.UnregisterCharacter(this);
        ws?.Close();
        ws = null;
    }

    protected virtual void OnApplicationQuit()
    {
        LiveLlmManager.Instance?.UnregisterCharacter(this);
        ws?.Close();
        ws = null;
    }

    protected virtual void OnTriggerEnter(Collider other)
    {
        if (!other.CompareTag("Player")) return;

        Debug.Log($"[{characterId}] Player ENTER.");
        StartConversation();
    }

    protected virtual void OnTriggerExit(Collider other)
    {
        if (!other.CompareTag("Player")) return;

        Debug.Log($"[{characterId}] Player EXIT.");
        StopConversation();
    }

    protected void StartConversation()
    {
        if (isConversationActive)
        {
            Debug.Log($"[{characterId}] Conversation already active.");
            return;
        }

        if (ws == null || ws.State != WebSocketState.Open)
        {
            Debug.LogWarning($"[{characterId}] WebSocket not ready.");
            return;
        }

        isConversationActive = true;
        LiveLlmManager.Instance?.RegisterCharacter(this);
        Debug.Log($"[{characterId}] Conversation started.");
    }

    protected void StopConversation()
    {
        if (!isConversationActive) return;

        isConversationActive = false;
        LiveLlmManager.Instance?.UnregisterCharacter(this);
        Debug.Log($"[{characterId}] Conversation stopped.");
        ForceStopTts(true);
    }

    public void SendMicFrame(byte[] frame, float[] floatChunk)
    {
        if (!isConversationActive || ws == null || ws.State != WebSocketState.Open) return;
        _ = ws.Send(frame);

        if (monitorSource != null && floatChunk != null)
        {
            var clip = AudioClip.Create($"{characterId}_MicChunk", floatChunk.Length, 1, SampleRate, false);
            clip.SetData(floatChunk, 0);
            monitorSource.PlayOneShot(clip);
        }
    }

    private void HandleServerMessage(byte[] bytes)
    {
        if (bytes == null || bytes.Length == 0) return;

        string json;
        try
        {
            json = Encoding.UTF8.GetString(bytes);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[{characterId}] Failed to decode message: {ex.Message}");
            return;
        }

        ServerMessage msg;
        try
        {
            msg = JsonUtility.FromJson<ServerMessage>(json);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[{characterId}] Invalid JSON: {ex.Message} | {json}");
            return;
        }

        if (!string.IsNullOrEmpty(msg.character_id) &&
            !string.Equals(msg.character_id, characterId, StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        switch (msg.type)
        {
            case "partial_user_request":
            case "final_user_request":
            case "partial_assistant_answer":
            case "final_assistant_answer":
                Debug.Log($"[{characterId}] {msg.type}: {msg.content}");
                break;

            case "tts_chunk":
                PlayTtsChunk(msg.content);
                break;

            case "tts_interruption":
                ForceStopTts(false);
                break;

            case "stop_tts":
                ForceStopTts(true);
                break;

            case "character_ready":
                // Server signals character is fully initialized
                Debug.Log($"[{characterId}] Character ready signal received.");
                break;

            default:
                Debug.Log($"[{characterId}] Unhandled message type '{msg.type}'.");
                break;
        }
    }

    /// <summary>
    /// Play TTS audio from external source (e.g., NPC conversations).
    /// Called by NpcConversationController to route audio to this character.
    /// </summary>
    public void PlayExternalTtsChunk(string base64Content)
    {
        PlayTtsChunk(base64Content);
    }

    private void PlayTtsChunk(string base64Content)
    {
        if (string.IsNullOrEmpty(base64Content) || !ttsSource) return;

        byte[] pcmBytes;
        try
        {
            pcmBytes = Convert.FromBase64String(base64Content);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[{characterId}] Invalid base64 chunk: {ex.Message}");
            return;
        }

        int sampleCount = pcmBytes.Length / 2;
        if (sampleCount == 0) return;

        float[] samples = new float[sampleCount];
        for (int i = 0; i < sampleCount; i++)
        {
            short s = BitConverter.ToInt16(pcmBytes, i * 2);
            samples[i] = Mathf.Clamp(s / 32768f, -1f, 1f);
        }

        EnqueueTtsSamples(samples);
        MarkTtsPlaying(sampleCount / (float)SampleRate);
    }

    private void EnqueueTtsSamples(float[] samples)
    {
        lock (ttsBufferLock)
        {
            foreach (float sample in samples)
            {
                ttsBuffer[ttsWriteIndex] = sample;
                ttsWriteIndex = (ttsWriteIndex + 1) % ttsBuffer.Length;

                if (ttsBufferedSamples < ttsBuffer.Length)
                {
                    ttsBufferedSamples++;
                }
                else
                {
                    ttsReadIndex = (ttsReadIndex + 1) % ttsBuffer.Length;
                }
            }
        }
    }

    private void OnAudioRead(float[] data)
    {
        int filled = 0;

        lock (ttsBufferLock)
        {
            while (filled < data.Length && ttsBufferedSamples > 0)
            {
                data[filled] = ttsBuffer[ttsReadIndex];
                ttsReadIndex = (ttsReadIndex + 1) % ttsBuffer.Length;
                ttsBufferedSamples--;
                filled++;
            }
        }

        while (filled < data.Length)
        {
            data[filled++] = 0f;
        }
    }

    private void OnSetAudioPosition(int newPosition) { }

    private void MarkTtsPlaying(float chunkDurationSeconds)
    {
        if (!isTtsPlayingClient)
        {
            isTtsPlayingClient = true;
            SendJsonMessage("tts_start");
        }

        if (ttsStopCoroutine != null)
        {
            StopCoroutine(ttsStopCoroutine);
        }

        float delay = Mathf.Max(chunkDurationSeconds + TtsStopGrace, TtsStopMinDelay);
        ttsStopCoroutine = StartCoroutine(TtsStopWatchdog(delay));
    }

    private IEnumerator TtsStopWatchdog(float delay)
    {
        yield return new WaitForSeconds(delay);
        ttsStopCoroutine = null;
        if (isTtsPlayingClient)
        {
            isTtsPlayingClient = false;
            SendJsonMessage("tts_stop");
        }
    }

    private void ForceStopTts(bool notifyServer)
    {
        if (ttsStopCoroutine != null)
        {
            StopCoroutine(ttsStopCoroutine);
            ttsStopCoroutine = null;
        }

        lock (ttsBufferLock)
        {
            ttsBufferedSamples = 0;
            ttsReadIndex = 0;
            ttsWriteIndex = 0;
            Array.Clear(ttsBuffer, 0, ttsBuffer.Length);
        }

        if (isTtsPlayingClient)
        {
            isTtsPlayingClient = false;
            if (notifyServer)
            {
                SendJsonMessage("tts_stop");
            }
        }
    }

    private async void SendJsonMessage(string type, string content = "")
    {
        if (ws == null || ws.State != WebSocketState.Open) return;

        var payload = new ClientMessage
        {
            type = type,
            content = content ?? string.Empty,
            character_id = characterId
        };

        string json = JsonUtility.ToJson(payload);
        try
        {
            await ws.SendText(json);
        }
        catch (Exception ex)
        {
            Debug.LogError($"[{characterId}] Failed to send JSON message: {ex.Message}");
        }
    }
}