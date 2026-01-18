using System;
using System.Collections;
using System.Collections.Generic;
using UnityEngine;

/// <summary>
/// NPC Injection Trigger System - Automatic NPC-to-NPC conversations.
/// 
/// How it works:
/// - Continuity and Self-Directed triggers are ALWAYS ON (fallback)
/// - Location triggers (Sudoku, Kitchen) override fallbacks when active
/// - Timer starts AFTER NPCs finish talking (20-40s between conversations)
/// - Suppressed when player is engaged (in NPC zone or speaking)
/// 
/// Setup:
/// 1. Add this to a GameObject in the scene
/// 2. Create trigger zones with NpcInjectionTriggerZone component
/// 3. System handles the rest automatically
/// </summary>
public class NpcInjectionTriggerSystem : MonoBehaviour
{
    public static NpcInjectionTriggerSystem Instance { get; private set; }

    [Header("Timing")]
    [SerializeField] private float baseIntervalSeconds = 20f;
    [SerializeField] private float randomOffsetMaxSeconds = 20f;
    [SerializeField] private float engagementBufferSeconds = 5f;
    
    [Header("Characters")]
    [SerializeField] private string npc1Id = "LisaParker";
    [SerializeField] private string npc2Id = "PaulAdams";
    
    [Header("Debug")]
    [SerializeField] private bool debugLogging = true;

    // Trigger states - Location triggers are OFF by default, Fallbacks are ALWAYS ON
    private bool triggerSudoku = false;
    private bool triggerKitchen = false;
    // Continuity and SelfDirected are always on - no variables needed

    // Player engagement state
    private bool playerInNpcZone = false;
    private float playerEngagedUntil = 0f;
    
    // Conversation state
    private bool npcConversationRunning = false;
    private bool waitingForConversationEnd = false;
    
    // Timer
    private float nextTickTime = 0f;
    private Coroutine timerCoroutine;

    #region Injection Categories & Prompts

    private class InjectionCategory
    {
        public string id;
        public string name;
        public bool isMonologue;
        public bool isLocationBased;
        public List<string> prompts;
    }

    private Dictionary<string, InjectionCategory> categories;

    private void InitializeCategories()
    {
        categories = new Dictionary<string, InjectionCategory>
        {
            ["sudoku"] = new InjectionCategory
            {
                id = "sudoku",
                name = "Sudoku",
                isMonologue = false,
                isLocationBased = true,
                prompts = new List<string>
                {
                    "Start a brief exchange about staying patient and looking for patterns in the Sudoku, explicitly anchored to the most recent relevant conversational cue (paraphrase only), without giving any actionable steps or numbers.",
                    "Start a brief exchange about not rushing the Sudoku and avoiding forcing progress when nothing is clear, explicitly tied to the current tone in the last few turns, without giving any solving advice.",
                    "Start a brief exchange about how stress affects focus on puzzles, grounded in the pacing or tension that just occurred in the conversation, keeping it general and not addressing the player directly.",
                    "Start a brief exchange about how people sometimes over-read meaning into small puzzle details, explicitly responding to the most recent strong wording or assumption (paraphrase only), without giving any solving guidance."
                }
            },
            ["kitchen_safe"] = new InjectionCategory
            {
                id = "kitchen_safe",
                name = "Kitchen",
                isMonologue = false,
                isLocationBased = true,
                prompts = new List<string>
                {
                    "Start a brief exchange about a safe in a kitchen feeling unusual, explicitly anchored to the most recent conversational cue (paraphrase only), without guessing what is inside or why it is there.",
                    "Start a brief exchange where one agent frames a kitchen safe as normal and the other frames it as odd, keeping it mild and non-accusatory.",
                    "Start a brief exchange reminding each other not to assume meaning from the safe until it is opened, without giving procedural instructions.",
                    "Start a brief exchange about how unusual objects can pull attention away from the bigger picture, without introducing new facts."
                }
            },
            ["continuity"] = new InjectionCategory
            {
                id = "continuity",
                name = "Continuity",
                isMonologue = false,
                isLocationBased = false,
                prompts = new List<string>
                {
                    "Continue your current topic for one short back-and-forth when there is no new input, explicitly referencing the last unresolved point that was just raised (paraphrase only), then stop naturally.",
                    "Resume an earlier topic as if it was ongoing, picking up from the last unresolved point (paraphrase only), without greeting or asking a question.",
                    "Have one short check-in with each other about staying calm and thinking clearly, tied to the quiet moment that just happened, without mentioning the player."
                }
            },
            ["self_directed"] = new InjectionCategory
            {
                id = "self_directed",
                name = "Self-Directed",
                isMonologue = true,
                isLocationBased = false,
                prompts = new List<string>
                {
                    "Mutter to yourself something like 'Stay calm... just stay calm.' or 'Keep it together.' - a short self-reassurance about staying composed.",
                    "Mutter to yourself something like 'I don't know what to think anymore...' or 'Nothing makes sense.' - expressing doubt and uncertainty.",
                    "Mutter to yourself something like 'This tension is unbearable...' or 'Why does everything feel so heavy?' - commenting on the tense atmosphere.",
                    "Mutter to yourself something like 'I should watch what I say...' or 'Careful now...' - reminding yourself to be cautious with words."
                }
            }
        };
    }

    #endregion

    #region Unity Lifecycle

    private void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Destroy(gameObject);
            return;
        }
        Instance = this;
        DontDestroyOnLoad(gameObject);
        
        InitializeCategories();
    }

    private void Start()
    {
        // Disable old auto-trigger system
        var npcController = NpcConversationController.Instance;
        if (npcController != null)
        {
            npcController.SetAutoTrigger(false);
            
            // Subscribe to conversation events
            npcController.OnConversationEnded.AddListener(OnNpcConversationEnded);
            npcController.OnConversationStarted.AddListener(OnNpcConversationStarted);
        }
        
        Log("System initialized - Auto-injection ACTIVE");
        Log("Fallback triggers (Continuity, Self-Directed) are ALWAYS ON");
        Log("Location triggers (Sudoku, Kitchen) override fallbacks when active");
        
        // Start the injection loop
        StartTimer();
    }

    private void OnDestroy()
    {
        if (timerCoroutine != null)
        {
            StopCoroutine(timerCoroutine);
        }
        
        var npcController = NpcConversationController.Instance;
        if (npcController != null)
        {
            npcController.OnConversationEnded.RemoveListener(OnNpcConversationEnded);
            npcController.OnConversationStarted.RemoveListener(OnNpcConversationStarted);
        }
    }

    #endregion

    #region Public API - Trigger Control

    /// <summary>
    /// Set Sudoku trigger (player at Sudoku table).
    /// </summary>
    public void SetSudokuTrigger(bool active)
    {
        if (triggerSudoku != active)
        {
            triggerSudoku = active;
            Log($"Sudoku trigger: {(active ? "ON" : "OFF")}");
        }
    }

    /// <summary>
    /// Set Kitchen trigger (player in kitchen).
    /// </summary>
    public void SetKitchenTrigger(bool active)
    {
        if (triggerKitchen != active)
        {
            triggerKitchen = active;
            Log($"Kitchen trigger: {(active ? "ON" : "OFF")}");
        }
    }

    /// <summary>
    /// Set player in NPC zone (suppresses injections).
    /// </summary>
    public void SetPlayerInNpcZone(bool inZone)
    {
        if (playerInNpcZone != inZone)
        {
            playerInNpcZone = inZone;
            Log($"Player in NPC zone: {(inZone ? "YES (suppressing)" : "NO")}");
        }
    }

    /// <summary>
    /// Called when player speaks (detected by voice activity).
    /// Suppresses injections until NPC responds + buffer.
    /// </summary>
    public void OnPlayerSpeaking()
    {
        // Set engaged until far in the future - will be updated when NPC responds
        playerEngagedUntil = Time.time + 60f;
        Log("Player speaking - engaged");
    }

    /// <summary>
    /// Called when NPC finishes responding to player.
    /// </summary>
    public void OnNpcFinishedSpeakingToPlayer()
    {
        // After NPC finishes, wait buffer time
        playerEngagedUntil = Time.time + engagementBufferSeconds;
        Log($"NPC done speaking to player - {engagementBufferSeconds}s buffer");
    }

    #endregion

    #region Engagement & Can-Inject Logic

    private bool CanInject()
    {
        // If player is in NPC zone, suppress
        if (playerInNpcZone) return false;
        
        // If player is engaged (speaking to NPCs), suppress
        if (Time.time < playerEngagedUntil) return false;
        
        return true;
    }

    #endregion

    #region Conversation Events

    private void OnNpcConversationStarted()
    {
        npcConversationRunning = true;
        waitingForConversationEnd = true;
        Log("NPC conversation started - waiting for end...");
    }

    private void OnNpcConversationEnded()
    {
        npcConversationRunning = false;
        
        if (waitingForConversationEnd)
        {
            waitingForConversationEnd = false;
            Log("NPC conversation ended - starting timer");
            StartTimer();
        }
    }

    #endregion

    #region Timer & Injection Loop

    private void StartTimer()
    {
        if (timerCoroutine != null)
        {
            StopCoroutine(timerCoroutine);
        }
        timerCoroutine = StartCoroutine(InjectionTimerCoroutine());
    }

    private IEnumerator InjectionTimerCoroutine()
    {
        // Calculate wait time
        float waitTime = baseIntervalSeconds + UnityEngine.Random.Range(0f, randomOffsetMaxSeconds);
        nextTickTime = Time.time + waitTime;
        Log($"Next tick in {waitTime:F1}s");
        
        yield return new WaitForSeconds(waitTime);
        
        // --- TICK ---
        Log("--- TICK ---");
        
        // Check if we can inject
        if (!CanInject())
        {
            if (playerInNpcZone)
                Log("Skipped: player in NPC zone");
            else if (Time.time < playerEngagedUntil)
                Log($"Skipped: player engaged ({playerEngagedUntil - Time.time:F1}s remaining)");
            else
                Log("Skipped: engagement");
            
            // Reschedule
            StartTimer();
            yield break;
        }
        
        // Check if conversation is running
        if (npcConversationRunning || (NpcConversationController.Instance?.IsConversationRunning ?? false))
        {
            Log("Skipped: conversation running");
            StartTimer();
            yield break;
        }
        
        // Find best trigger to fire
        var (categoryId, categoryName) = FindBestTrigger();
        
        if (categoryId != null)
        {
            Log($"🔥 FIRING: {categoryName}");
            bool success = ExecuteInjection(categoryId);
            
            if (success)
            {
                Log("Started! Waiting for conversation to end...");
                waitingForConversationEnd = true;
                // Don't restart timer - wait for conversation to end
                
                // Fallback timeout after 60s
                StartCoroutine(FallbackTimeout());
            }
            else
            {
                Log("Injection failed!");
                StartTimer();
            }
        }
        else
        {
            Log("No triggers ready");
            StartTimer();
        }
    }

    private IEnumerator FallbackTimeout()
    {
        yield return new WaitForSeconds(60f);
        
        if (waitingForConversationEnd)
        {
            Log("Fallback: conversation timeout, restarting timer");
            waitingForConversationEnd = false;
            npcConversationRunning = false;
            StartTimer();
        }
    }

    #endregion

    #region Trigger Selection

    private (string categoryId, string categoryName) FindBestTrigger()
    {
        // Priority: Location triggers > Fallback triggers
        // If ANY location trigger is active, ONLY use location triggers
        
        bool anyLocationActive = triggerSudoku || triggerKitchen;
        
        if (anyLocationActive)
        {
            // Only location triggers
            if (triggerSudoku)
                return ("sudoku", "Sudoku");
            if (triggerKitchen)
                return ("kitchen_safe", "Kitchen");
        }
        else
        {
            // Fallback triggers (always on) - randomly pick one
            if (UnityEngine.Random.value > 0.5f)
                return ("continuity", "Continuity");
            else
                return ("self_directed", "Self-Directed");
        }
        
        return (null, null);
    }

    #endregion

    #region Execute Injection

    private bool ExecuteInjection(string categoryId)
    {
        var npcController = NpcConversationController.Instance;
        if (npcController == null)
        {
            Debug.LogError("[NpcInjection] NpcConversationController not found!");
            return false;
        }
        
        if (npcController.IsConversationRunning)
        {
            return false;
        }
        
        if (!categories.TryGetValue(categoryId, out var category))
        {
            Debug.LogError($"[NpcInjection] Unknown category: {categoryId}");
            return false;
        }
        
        // Get random prompt
        string prompt = category.prompts[UnityEngine.Random.Range(0, category.prompts.Count)];
        
        // Get turns (2-4 for dialogue, 1 for monologue)
        int turns = category.isMonologue ? 1 : UnityEngine.Random.Range(2, 5);
        
        // Get NPCs
        string npc1 = npc1Id;
        string npc2 = category.isMonologue ? "" : npc2Id;
        
        Log($"Starting: {categoryId} ({(category.isMonologue ? "monologue" : turns + " turns")})");
        
        npcController.StartConversation(npc1, npc2, turns, prompt);
        return true;
    }

    #endregion

    #region Debug

    private void Log(string message)
    {
        if (debugLogging)
        {
            Debug.Log($"[NpcInjection] {message}");
        }
    }

    /// <summary>
    /// Get debug status string.
    /// </summary>
    public string GetDebugStatus()
    {
        bool anyLocation = triggerSudoku || triggerKitchen;
        int readyCount = anyLocation ? (triggerSudoku ? 1 : 0) + (triggerKitchen ? 1 : 0) : 2;
        int skipCount = anyLocation ? 2 : 0;
        
        float timeRemaining = Mathf.Max(0, nextTickTime - Time.time);
        
        string status = "";
        
        if (npcConversationRunning)
            status = "NPC conv running...";
        else if (playerInNpcZone)
            status = "Suppressed (NPC zone)";
        else if (Time.time < playerEngagedUntil)
            status = $"Engaged ({playerEngagedUntil - Time.time:F0}s)";
        else
            status = $"{timeRemaining:F0}s ({readyCount} ready" + (skipCount > 0 ? $", {skipCount} skip" : "") + ")";
        
        return status;
    }

    #endregion
}
