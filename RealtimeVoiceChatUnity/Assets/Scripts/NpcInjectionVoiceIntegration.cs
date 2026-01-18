using UnityEngine;

/// <summary>
/// Integrates voice activity detection with the NPC injection system.
/// Automatically detects when player is speaking and when NPCs respond.
/// 
/// Attach to the same GameObject as LiveLlmManager or any persistent object.
/// </summary>
public class NpcInjectionVoiceIntegration : MonoBehaviour
{
    [Header("References")]
    [SerializeField] private LiveLlmManager liveLlmManager;
    
    [Header("Debug")]
    [SerializeField] private bool debugLogging = true;

    private void Start()
    {
        // Find LiveLlmManager if not assigned
        if (liveLlmManager == null)
        {
            liveLlmManager = FindObjectOfType<LiveLlmManager>();
        }
        
        if (liveLlmManager != null)
        {
            // Subscribe to player speaking event
            liveLlmManager.OnPlayerSpeaking += HandlePlayerSpeaking;
            Log("Subscribed to LiveLlmManager.OnPlayerSpeaking");
        }
        else
        {
            Debug.LogWarning("[NpcInjectionVoice] LiveLlmManager not found - player speaking detection disabled");
        }
        
        // Subscribe to NPC conversation events
        var npcController = NpcConversationController.Instance;
        if (npcController != null)
        {
            // When NPC responds to player (conversation turn in player-NPC conversation)
            // Note: This is for detecting when NPC finishes speaking TO THE PLAYER, not NPC-to-NPC
            npcController.OnConversationTurn.AddListener(HandleNpcTurn);
        }
    }

    private void OnDestroy()
    {
        if (liveLlmManager != null)
        {
            liveLlmManager.OnPlayerSpeaking -= HandlePlayerSpeaking;
        }
        
        var npcController = NpcConversationController.Instance;
        if (npcController != null)
        {
            npcController.OnConversationTurn.RemoveListener(HandleNpcTurn);
        }
    }

    private void HandlePlayerSpeaking()
    {
        var system = NpcInjectionTriggerSystem.Instance;
        if (system != null)
        {
            system.OnPlayerSpeaking();
            Log("Player speaking detected - notified injection system");
        }
    }

    private void HandleNpcTurn(string speakerId, string message)
    {
        // This is called for NPC-to-NPC conversations, but we want to detect
        // when NPC finishes speaking TO THE PLAYER
        // For now, we'll use a different approach - check if any character just finished TTS
    }

    /// <summary>
    /// Call this when an NPC finishes speaking to the player (from LiveLlmCharacterBase or similar).
    /// </summary>
    public static void NotifyNpcFinishedSpeakingToPlayer()
    {
        var system = NpcInjectionTriggerSystem.Instance;
        if (system != null)
        {
            system.OnNpcFinishedSpeakingToPlayer();
        }
    }

    private void Log(string message)
    {
        if (debugLogging)
        {
            Debug.Log($"[NpcInjectionVoice] {message}");
        }
    }
}
