using UnityEngine;
using System;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Globalization;

/// <summary>
/// Logs all player interactions to file for analytics.
/// Tracks: Zone entries/exits, NPC conversations, time spent in areas.
/// </summary>
public class PlayerInteractionLogger : MonoBehaviour
{
    public static PlayerInteractionLogger Instance { get; private set; }

    [Header("Logging Settings")]
    [SerializeField] private bool enableFileLogging = true;
    [SerializeField] private bool enableConsoleLogging = true;
    [SerializeField] private string logFileName = "player_interactions";
    
    [Header("Debug")]
    [SerializeField] private bool verboseLogging = true;

    private string logFilePath;
    private StreamWriter logWriter;
    private StringBuilder pendingLogs = new StringBuilder();
    
    // Analytics tracking
    private Dictionary<string, float> zoneEnterTimes = new Dictionary<string, float>();
    private Dictionary<string, float> totalTimeInZone = new Dictionary<string, float>();
    private int npcConversationCount = 0;
    private int playerNpcInteractionCount = 0;
    private float sessionStartTime;
    
    // Current state
    private bool inSudokuZone = false;
    private bool inKitchenZone = false;
    private bool inNpcZone = false;

    private void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Destroy(gameObject);
            return;
        }
        Instance = this;
        DontDestroyOnLoad(gameObject);
        
        sessionStartTime = Time.time;
        InitializeLogFile();
    }

    private void InitializeLogFile()
    {
        if (!enableFileLogging) return;
        
        try
        {
            // Create logs directory NEXT TO the game executable (or in project root in Editor)
            // This makes it easy to find across multiple setups
            string baseDir;
            
            #if UNITY_EDITOR
            // In Editor: use project root (parent of Assets)
            baseDir = Path.GetDirectoryName(Application.dataPath);
            #else
            // In Build: use the folder where the .exe is
            baseDir = Path.GetDirectoryName(Application.dataPath);
            #endif
            
            string logDir = Path.Combine(baseDir, "PlayerLogs");
            if (!Directory.Exists(logDir))
            {
                Directory.CreateDirectory(logDir);
            }
            
            // Create unique filename with GLOBAL timestamp
            string timestamp = DateTime.Now.ToString("yyyy-MM-dd_HH-mm-ss");
            logFilePath = Path.Combine(logDir, $"{logFileName}_{timestamp}.csv");
            
            // Create file and write header with global timestamp info
            logWriter = new StreamWriter(logFilePath, false, Encoding.UTF8);
            logWriter.WriteLine($"# Session started: {DateTime.Now:yyyy-MM-dd HH:mm:ss} (UTC: {DateTime.UtcNow:yyyy-MM-dd HH:mm:ss})");
            logWriter.WriteLine($"# Machine: {Environment.MachineName}");
            logWriter.WriteLine("GlobalTime,SessionTime,EventType,Zone,Details,Duration");
            logWriter.Flush();
            
            Debug.Log($"[PlayerLogger] 📁 Log file: {logFilePath}");
        }
        catch (Exception e)
        {
            Debug.LogError($"[PlayerLogger] Failed to create log file: {e.Message}");
            enableFileLogging = false;
        }
    }

    private void OnDestroy()
    {
        // Write session summary before closing
        WriteSessionSummary();
        
        if (logWriter != null)
        {
            logWriter.Close();
            logWriter = null;
        }
    }

    private void OnApplicationQuit()
    {
        WriteSessionSummary();
        
        if (logWriter != null)
        {
            logWriter.Close();
            logWriter = null;
        }
    }

    #region Public API - Zone Events
    
    public void LogZoneEnter(string zoneName)
    {
        float currentTime = Time.time;
        zoneEnterTimes[zoneName] = currentTime;
        
        // Update state
        switch (zoneName.ToLower())
        {
            case "sudoku": inSudokuZone = true; break;
            case "kitchen": inKitchenZone = true; break;
            case "npcengagement": inNpcZone = true; break;
        }
        
        string details = $"Player entered {zoneName} zone";
        LogEvent("ZONE_ENTER", zoneName, details, 0);
        
        if (enableConsoleLogging)
        {
            Debug.Log($"[PlayerLogger] 📍 ENTER: {zoneName} zone at {currentTime:F1}s");
        }
    }
    
    public void LogZoneExit(string zoneName)
    {
        float currentTime = Time.time;
        float duration = 0;
        
        if (zoneEnterTimes.TryGetValue(zoneName, out float enterTime))
        {
            duration = currentTime - enterTime;
            
            // Accumulate total time
            if (!totalTimeInZone.ContainsKey(zoneName))
                totalTimeInZone[zoneName] = 0;
            totalTimeInZone[zoneName] += duration;
        }
        
        // Update state
        switch (zoneName.ToLower())
        {
            case "sudoku": inSudokuZone = false; break;
            case "kitchen": inKitchenZone = false; break;
            case "npcengagement": inNpcZone = false; break;
        }
        
        string details = string.Format(CultureInfo.InvariantCulture, "Player left {0} zone (was there {1:F1}s)", zoneName, duration);
        LogEvent("ZONE_EXIT", zoneName, details, duration);
        
        if (enableConsoleLogging)
        {
            Debug.Log($"[PlayerLogger] 📍 EXIT: {zoneName} zone after {duration:F1}s");
        }
    }
    
    #endregion
    
    #region Public API - NPC Events
    
    public void LogNpcConversationStart(string npc1, string npc2, string trigger, string prompt)
    {
        npcConversationCount++;
        
        string details = $"{npc1} <-> {npc2} | Trigger: {trigger} | Prompt: {TruncateString(prompt, 50)}";
        LogEvent("NPC_CONV_START", trigger, details, 0);
        
        if (enableConsoleLogging)
        {
            Debug.Log($"[PlayerLogger] 💬 NPC Conv #{npcConversationCount}: {npc1} <-> {npc2} ({trigger})");
        }
    }
    
    public void LogNpcConversationEnd(string trigger, int turns)
    {
        string details = $"Conversation ended after {turns} turns";
        LogEvent("NPC_CONV_END", trigger, details, 0);
        
        if (enableConsoleLogging)
        {
            Debug.Log($"[PlayerLogger] 💬 NPC Conv ended: {turns} turns");
        }
    }
    
    public void LogPlayerNpcInteraction(string npcName, string playerMessage)
    {
        playerNpcInteractionCount++;
        
        string details = $"Player spoke to {npcName}: {TruncateString(playerMessage, 100)}";
        LogEvent("PLAYER_NPC_TALK", npcName, details, 0);
        
        if (enableConsoleLogging && verboseLogging)
        {
            Debug.Log($"[PlayerLogger] 🗣️ Player -> {npcName}: {TruncateString(playerMessage, 50)}");
        }
    }
    
    public void LogPlayerSpeaking()
    {
        LogEvent("PLAYER_SPEAKING", "", "Player voice activity detected", 0);
    }
    
    #endregion
    
    #region Public API - Injection Events
    
    public void LogInjectionFired(string triggerName, string prompt)
    {
        string details = $"Injection fired: {TruncateString(prompt, 80)}";
        LogEvent("INJECTION_FIRED", triggerName, details, 0);
        
        if (enableConsoleLogging)
        {
            Debug.Log($"[PlayerLogger] 🔥 INJECTION: {triggerName}");
        }
    }
    
    public void LogInjectionSkipped(string reason)
    {
        LogEvent("INJECTION_SKIPPED", "", reason, 0);
        
        if (enableConsoleLogging && verboseLogging)
        {
            Debug.Log($"[PlayerLogger] ⏭️ Injection skipped: {reason}");
        }
    }
    
    #endregion
    
    #region Core Logging
    
    private void LogEvent(string eventType, string zone, string details, float duration)
    {
        if (!enableFileLogging || logWriter == null) return;
        
        try
        {
            // GLOBAL timestamp (real world time)
            string globalTime = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff");
            float sessionTime = Time.time - sessionStartTime;
            
            // Escape CSV fields
            string safeDetails = details.Replace("\"", "\"\"").Replace(",", ";");
            
            // Use InvariantCulture to ensure dots as decimal separators (not commas!)
            string line = string.Format(CultureInfo.InvariantCulture, 
                "{0},{1:F2},{2},{3},\"{4}\",{5:F2}",
                globalTime, sessionTime, eventType, zone, safeDetails, duration);
            
            logWriter.WriteLine(line);
            logWriter.Flush(); // Ensure immediate write
        }
        catch (Exception e)
        {
            Debug.LogError($"[PlayerLogger] Write error: {e.Message}");
        }
    }
    
    private void WriteSessionSummary()
    {
        if (!enableFileLogging || logWriter == null) return;
        
        try
        {
            float totalSessionTime = Time.time - sessionStartTime;
            
            // Calculate total time in tracked zones
            float totalTrackedTime = 0f;
            foreach (var kvp in totalTimeInZone)
            {
                totalTrackedTime += kvp.Value;
            }
            
            // "Other" = time not in any tracked zone
            float otherTime = Mathf.Max(0, totalSessionTime - totalTrackedTime);
            
            // Use InvariantCulture for all numbers!
            var ci = CultureInfo.InvariantCulture;
            
            logWriter.WriteLine("");
            logWriter.WriteLine("=== SESSION SUMMARY ===");
            logWriter.WriteLine(string.Format(ci, "Total Session Time: {0:F1}s ({1:F1} minutes)", totalSessionTime, totalSessionTime/60));
            logWriter.WriteLine($"NPC-to-NPC Conversations: {npcConversationCount}");
            logWriter.WriteLine($"Player-NPC Interactions: {playerNpcInteractionCount}");
            logWriter.WriteLine("");
            logWriter.WriteLine("Time in Zones:");
            
            // Show each tracked zone
            foreach (var kvp in totalTimeInZone)
            {
                float percentage = (kvp.Value / totalSessionTime) * 100;
                logWriter.WriteLine(string.Format(ci, "  {0}: {1:F1}s ({2:F1}%)", kvp.Key, kvp.Value, percentage));
            }
            
            // Show "Other" time
            float otherPercentage = (otherTime / totalSessionTime) * 100;
            logWriter.WriteLine(string.Format(ci, "  Other: {0:F1}s ({1:F1}%)", otherTime, otherPercentage));
            
            logWriter.Flush();
            
            Debug.Log(string.Format(ci, "[PlayerLogger] 📊 SESSION SUMMARY: {0:F1}min, {1} NPC convs, {2} player interactions", 
                totalSessionTime/60, npcConversationCount, playerNpcInteractionCount));
        }
        catch (Exception e)
        {
            Debug.LogError($"[PlayerLogger] Summary write error: {e.Message}");
        }
    }
    
    private string TruncateString(string input, int maxLength)
    {
        if (string.IsNullOrEmpty(input)) return "";
        return input.Length <= maxLength ? input : input.Substring(0, maxLength) + "...";
    }
    
    #endregion
    
    #region Debug Info
    
    public string GetCurrentState()
    {
        return $"Sudoku:{inSudokuZone} Kitchen:{inKitchenZone} NpcZone:{inNpcZone}";
    }
    
    public string GetStats()
    {
        float sessionTime = Time.time - sessionStartTime;
        return $"Session: {sessionTime/60:F1}min | NPCConvs: {npcConversationCount} | PlayerTalks: {playerNpcInteractionCount}";
    }
    
    #endregion
}
