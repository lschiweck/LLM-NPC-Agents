using UnityEngine;

/// <summary>
/// Simple trigger zone for NPC injection system.
/// Attach to a GameObject with a Collider (set to "Is Trigger").
/// 
/// Zone Types:
/// - Sudoku: Activates Sudoku trigger when player enters
/// - Kitchen: Activates Kitchen trigger when player enters
/// - NpcEngagement: Suppresses all injections when player is near NPCs
/// </summary>
[RequireComponent(typeof(Collider))]
public class NpcInjectionZone : MonoBehaviour
{
    public enum ZoneType
    {
        Sudoku,         // Player at Sudoku table
        Kitchen,        // Player in kitchen
        NpcEngagement   // Player near NPCs (suppresses injections)
    }

    [Header("Zone Settings")]
    [SerializeField] private ZoneType zoneType = ZoneType.NpcEngagement;
    
    [Header("Player Detection")]
    [SerializeField] private string playerTag = "Player";
    
    [Header("Debug")]
    [SerializeField] private bool showDebugGizmo = true;
    [SerializeField] private Color gizmoColor = new Color(1f, 0.5f, 0f, 0.3f);

    private void OnTriggerEnter(Collider other)
    {
        if (!other.CompareTag(playerTag)) return;
        
        // Log FIRST so we always see it
        Debug.Log($"[NpcInjectionZone] Player ENTERED {zoneType} zone");
        
        var system = NpcInjectionTriggerSystem.Instance;
        if (system == null)
        {
            Debug.LogWarning("[NpcInjectionZone] NpcInjectionTriggerSystem.Instance is NULL!");
            return;
        }
        
        switch (zoneType)
        {
            case ZoneType.Sudoku:
                system.SetSudokuTrigger(true);
                break;
                
            case ZoneType.Kitchen:
                system.SetKitchenTrigger(true);
                break;
                
            case ZoneType.NpcEngagement:
                system.SetPlayerInNpcZone(true);
                break;
        }
    }

    private void OnTriggerExit(Collider other)
    {
        if (!other.CompareTag(playerTag)) return;
        
        // Log FIRST so we always see it
        Debug.Log($"[NpcInjectionZone] Player LEFT {zoneType} zone");
        
        var system = NpcInjectionTriggerSystem.Instance;
        if (system == null)
        {
            Debug.LogWarning("[NpcInjectionZone] NpcInjectionTriggerSystem.Instance is NULL!");
            return;
        }
        
        switch (zoneType)
        {
            case ZoneType.Sudoku:
                system.SetSudokuTrigger(false);
                break;
                
            case ZoneType.Kitchen:
                system.SetKitchenTrigger(false);
                break;
                
            case ZoneType.NpcEngagement:
                system.SetPlayerInNpcZone(false);
                break;
        }
    }

    private void OnDrawGizmos()
    {
        if (!showDebugGizmo) return;
        
        // Set color based on zone type
        Color color = zoneType switch
        {
            ZoneType.Sudoku => new Color(0f, 1f, 0f, 0.3f),      // Green
            ZoneType.Kitchen => new Color(1f, 0.5f, 0f, 0.3f),   // Orange
            ZoneType.NpcEngagement => new Color(1f, 0f, 0f, 0.3f), // Red
            _ => gizmoColor
        };
        
        Gizmos.color = color;
        
        var collider = GetComponent<Collider>();
        if (collider is BoxCollider box)
        {
            Gizmos.matrix = transform.localToWorldMatrix;
            Gizmos.DrawCube(box.center, box.size);
            Gizmos.DrawWireCube(box.center, box.size);
        }
        else if (collider is SphereCollider sphere)
        {
            Gizmos.DrawSphere(transform.position + sphere.center, sphere.radius);
            Gizmos.DrawWireSphere(transform.position + sphere.center, sphere.radius);
        }
    }
}
