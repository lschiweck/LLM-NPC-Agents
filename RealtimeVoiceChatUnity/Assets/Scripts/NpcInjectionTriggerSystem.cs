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
    [Tooltip("Log status every X seconds (0 = disabled)")]
    [SerializeField] private float periodicStatusLogInterval = 0f;

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
        // NOTE: All prompts are PLAYER-FOCUSED - NPCs must reference what the detective discussed
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
                    "The detective seems interested in David's Sudoku. Start a brief exchange about why the detective is looking at it. Reference something specific the detective said or asked recently. Don't give solving advice - just react to their interest.",
                    "React to what the detective has been asking. Start a brief exchange sharing a memory about David and his puzzle obsession. Tie it to whatever topic the detective was just discussing.",
                    "One of you finds the detective's focus on the Sudoku suspicious or interesting. Start a brief exchange about what the detective might be looking for, referencing their recent questions.",
                    "React to the detective's investigation. Start a brief exchange wondering if the Sudoku connects to something the detective asked about earlier."
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
                    "The detective is in the kitchen. Start a brief exchange about why they're looking around there. Reference what they asked you about recently - are they suspicious of someone? Looking for something?",
                    "Start a brief exchange wondering what the detective already knows about the safe. Connect it to their recent questions - did they ask about David's secrets? About who had access?",
                    "React to being questioned. Start a brief exchange about the detective's investigation style - are they close to finding something? Reference a specific question or accusation they made.",
                    "One of you is nervous about what the detective might find. Start a brief exchange about the safe and how it connects to something the detective asked about."
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
                    "The detective stepped away. Start a brief exchange about something specific they asked or said. Do they suspect one of you? What were they getting at?",
                    "Start a brief exchange about whether the detective suspects you or the other person. Reference a specific question or look they gave. What did they mean by that?",
                    "Start a brief exchange worrying about what the detective already knows. Reference something they asked about - do they know about the argument? The safe? Who was drunk?",
                    "Start a brief exchange about the detective's questioning strategy. Are they trying to catch you in a lie? Reference something inconsistent that came up.",
                    "Start a brief exchange processing the detective's questions. Pick one specific thing they asked about and discuss what you should have said or what you're worried about.",
                    "Start a brief exchange comparing what you each told the detective. Did your stories match? Reference specific details they asked about."
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
                    "Mutter to yourself about something the detective asked. What did they mean? Why did they ask that specifically?",
                    "Mutter to yourself about what the detective might find out. Reference something from the party or the investigation.",
                    "Mutter to yourself about someone the detective asked about - Olivia, Chris, or the other suspect. What do they know?",
                    "Mutter to yourself about something you said to the detective. Should you have said that? Will they figure it out?"
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
        
        // Start periodic status log if enabled
        if (periodicStatusLogInterval > 0)
        {
            StartCoroutine(PeriodicStatusLog());
        }
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
    /// Just extends the engagement window slightly - doesn't block for ages.
    /// </summary>
    public void OnPlayerSpeaking()
    {
        // Only extend if we're not already engaged for longer
        // This gives a 8 second window after the player stops speaking
        float newEngagedTime = Time.time + 8f;
        if (newEngagedTime > playerEngagedUntil)
        {
            playerEngagedUntil = newEngagedTime;
            // Don't spam logs - only log occasionally
        }
    }

    /// <summary>
    /// Called when NPC finishes responding to player.
    /// Resets engagement to just a short buffer.
    /// </summary>
    public void OnNpcFinishedSpeakingToPlayer()
    {
        // After NPC finishes, just a short buffer then ready
        playerEngagedUntil = Time.time + engagementBufferSeconds;
        Log($"NPC done - {engagementBufferSeconds}s buffer");
    }
    
    /// <summary>
    /// Reset engagement immediately (for testing/debug).
    /// </summary>
    public void ResetEngagement()
    {
        playerEngagedUntil = 0f;
        Log("Engagement reset");
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
        // Prevent duplicate end events
        if (!npcConversationRunning) return;
        
        npcConversationRunning = false;
        
        // Log to analytics (only once per conversation)
        if (PlayerInteractionLogger.Instance != null && !string.IsNullOrEmpty(lastFiredTrigger))
        {
            PlayerInteractionLogger.Instance.LogNpcConversationEnd(lastFiredTrigger, 0);
            lastFiredTrigger = ""; // Clear to prevent duplicate logging
        }
        
        if (waitingForConversationEnd)
        {
            waitingForConversationEnd = false;
            Log("NPC conversation ended - starting timer");
            StartTimer();
        }
    }
    
    private string lastFiredTrigger = "";

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
        
        // Fallback triggers (always on) - randomly pick one
        if (UnityEngine.Random.value > 0.5f)
            return ("continuity", "Continuity");
        else
            return ("self_directed", "Self-Directed");
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
        
        // Track for end logging
        lastFiredTrigger = categoryId;
        
        // Log to analytics file
        if (PlayerInteractionLogger.Instance != null)
        {
            PlayerInteractionLogger.Instance.LogNpcConversationStart(npc1, npc2, categoryId, prompt);
        }
        else
        {
            Debug.LogWarning("[NpcInjection] PlayerInteractionLogger not found! Analytics not recorded.");
        }
        
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
    
    private IEnumerator PeriodicStatusLog()
    {
        while (true)
        {
            yield return new WaitForSeconds(periodicStatusLogInterval);
            Debug.Log($"[NpcInjection] 📊 STATUS: Sudoku={triggerSudoku}, Kitchen={triggerKitchen}, InNpcZone={playerInNpcZone}, Engaged={(Time.time < playerEngagedUntil ? $"YES ({playerEngagedUntil - Time.time:F0}s)" : "NO")}");
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
