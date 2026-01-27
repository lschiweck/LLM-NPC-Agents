using System;
using System.Collections.Generic;
using UnityEngine;

public class LiveLlmManager : MonoBehaviour
{
    public static LiveLlmManager Instance { get; private set; }

    [Header("Capture Settings")]
    [SerializeField] private int sampleRate = 48_000;
    [SerializeField] private int chunkSamples = 2_048;
    [Tooltip("Optional mic device override; leave empty for default.")]
    [SerializeField] private string microphoneDeviceName = string.Empty;

    private const int HeaderBytes = 8;

    private readonly List<LiveLlmCharacterBase> listeners = new();

    private AudioClip micClip;
    private string micDevice;
    private float[] micReadBuffer = Array.Empty<float>();
    private float[] pendingSamples;
    private float[] chunkBuffer;
    private short[] pcmBuffer;

    private int lastSamplePosition;
    private int pendingCount;
    private bool capturing;
    private bool initializing;

    private void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Debug.LogWarning("[LiveLlmManager] Duplicate manager detected; destroying this instance.");
            Destroy(gameObject);
            return;
        }
        Instance = this;
        DontDestroyOnLoad(gameObject);
    }

    private void Start()
    {
        if (Microphone.devices.Length == 0)
        {
            Debug.LogError("[LiveLlmManager] No microphone detected; manager disabled.");
            enabled = false;
        }
    }

    public void RegisterCharacter(LiveLlmCharacterBase character)
    {
        if (!listeners.Contains(character))
        {
            listeners.Add(character);
        }
        if (!capturing)
        {
            StartCapture();
        }
    }

    public void UnregisterCharacter(LiveLlmCharacterBase character)
    {
        listeners.Remove(character);
        if (listeners.Count == 0)
        {
            StopCapture();
        }
    }

    private void Update()
    {
#if !UNITY_WEBGL || UNITY_EDITOR
        foreach (var listener in listeners)
        {
            listener.DispatchWebsocket();
        }
#endif
    }

    private void StartCapture()
    {
        if (capturing || initializing) return;
        micDevice = !string.IsNullOrEmpty(microphoneDeviceName)
            ? microphoneDeviceName
            : (Microphone.devices.Length > 0 ? Microphone.devices[0] : null);

        if (string.IsNullOrEmpty(micDevice))
        {
            Debug.LogError("[LiveLlmManager] Could not determine microphone device.");
            return;
        }

        StartCoroutine(InitializeCapture());
    }

    private void StopCapture()
    {
        if (!capturing && !initializing) return;

        capturing = false;
        initializing = false;
        Microphone.End(micDevice);
        micClip = null;
        pendingCount = 0;
        Debug.Log("[LiveLlmManager] Microphone capture stopped.");
    }

    private System.Collections.IEnumerator InitializeCapture()
    {
        initializing = true;
        micClip = Microphone.Start(micDevice, true, 1, sampleRate);

        float timeoutSeconds = 2f;
        float startTime = Time.realtimeSinceStartup;
        while (Microphone.GetPosition(micDevice) <= 0)
        {
            if (Time.realtimeSinceStartup - startTime > timeoutSeconds)
            {
                Debug.LogError("[LiveLlmManager] Microphone failed to start in time.");
                Microphone.End(micDevice);
                micClip = null;
                initializing = false;
                yield break;
            }
            yield return null;
        }

        micReadBuffer = new float[micClip.samples];
        pendingSamples = new float[chunkSamples];
        chunkBuffer = new float[chunkSamples];
        pcmBuffer = new short[chunkSamples];

        pendingCount = 0;
        lastSamplePosition = 0;
        capturing = true;
        initializing = false;

        StartCoroutine(CaptureLoop());
        Debug.Log("[LiveLlmManager] Microphone capture started.");
    }

    private System.Collections.IEnumerator CaptureLoop()
    {
        while (capturing)
        {
            int currentPosition = Microphone.GetPosition(micDevice);
            if (!Microphone.IsRecording(micDevice))
            {
                Debug.LogError("[LiveLlmManager] Microphone recording lost.");
                capturing = false;
                yield break;
            }

            int samplesAvailable = currentPosition - lastSamplePosition;
            if (samplesAvailable < 0)
            {
                samplesAvailable += micClip.samples;
            }

            if (samplesAvailable > 0)
            {
                samplesAvailable = Mathf.Min(samplesAvailable, micReadBuffer.Length);
                micClip.GetData(micReadBuffer, lastSamplePosition);

                int readOffset = 0;
                while (samplesAvailable > 0)
                {
                    int copyCount = Mathf.Min(chunkSamples - pendingCount, samplesAvailable);
                    Array.Copy(micReadBuffer, readOffset, pendingSamples, pendingCount, copyCount);
                    pendingCount += copyCount;
                    readOffset += copyCount;
                    samplesAvailable -= copyCount;
                    lastSamplePosition = (lastSamplePosition + copyCount) % micClip.samples;

                    if (pendingCount == chunkSamples)
                    {
                        Array.Copy(pendingSamples, chunkBuffer, chunkSamples);
                        pendingCount = 0;
                        BroadcastChunk(chunkBuffer);
                    }
                }
            }

            yield return null;
        }
    }

    private void BroadcastChunk(float[] floatChunk)
    {
        if (listeners.Count == 0) return;

        // Check if audio has significant volume (basic VAD)
        float maxAmp = 0f;
        for (int i = 0; i < chunkSamples; i++)
        {
            float sample = Mathf.Clamp(floatChunk[i], -1f, 1f);
            pcmBuffer[i] = (short)(sample < 0f ? sample * 32768f : sample * 32767f);
            if (Mathf.Abs(sample) > maxAmp) maxAmp = Mathf.Abs(sample);
        }

        // Notify injection system that player is speaking (if audio is above threshold)
        // This suppresses NPC-to-NPC conversations while player is actively talking
        if (maxAmp > 0.02f) // Threshold to filter out noise
        {
            NpcInjectionTriggerSystem.Instance?.OnPlayerSpeaking();
        }

        uint timestamp = (uint)(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() & 0xFFFFFFFF);
        int payloadBytes = pcmBuffer.Length * sizeof(short);

        foreach (var listener in listeners)
        {
            if (listener == null) continue;

            byte[] frame = new byte[HeaderBytes + payloadBytes];

            frame[0] = (byte)((timestamp >> 24) & 0xFF);
            frame[1] = (byte)((timestamp >> 16) & 0xFF);
            frame[2] = (byte)((timestamp >> 8) & 0xFF);
            frame[3] = (byte)(timestamp & 0xFF);

            uint flags = listener.IsTtsPlayingClient ? 1u : 0u;
            frame[4] = (byte)((flags >> 24) & 0xFF);
            frame[5] = (byte)((flags >> 16) & 0xFF);
            frame[6] = (byte)((flags >> 8) & 0xFF);
            frame[7] = (byte)(flags & 0xFF);

            Buffer.BlockCopy(pcmBuffer, 0, frame, HeaderBytes, payloadBytes);
            listener.SendMicFrame(frame, floatChunk);
        }
    }
}