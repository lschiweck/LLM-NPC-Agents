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
        
        var system = NpcInjectionTriggerSystem.Instance;
        if (system == null) return;
        
        switch (zoneType)
        {
            case ZoneType.Sudoku:
                system.SetSudokuTrigger(true);
                Debug.Log($"[NpcInjectionZone] Player entered SUDOKU zone");
                break;
                
            case ZoneType.Kitchen:
                system.SetKitchenTrigger(true);
                Debug.Log($"[NpcInjectionZone] Player entered KITCHEN zone");
                break;
                
            case ZoneType.NpcEngagement:
                system.SetPlayerInNpcZone(true);
                Debug.Log($"[NpcInjectionZone] Player entered NPC ENGAGEMENT zone (injections suppressed)");
                break;
        }
    }

    private void OnTriggerExit(Collider other)
    {
        if (!other.CompareTag(playerTag)) return;
        
        var system = NpcInjectionTriggerSystem.Instance;
        if (system == null) return;
        
        switch (zoneType)
        {
            case ZoneType.Sudoku:
                system.SetSudokuTrigger(false);
                Debug.Log($"[NpcInjectionZone] Player left SUDOKU zone");
                break;
                
            case ZoneType.Kitchen:
                system.SetKitchenTrigger(false);
                Debug.Log($"[NpcInjectionZone] Player left KITCHEN zone");
                break;
                
            case ZoneType.NpcEngagement:
                system.SetPlayerInNpcZone(false);
                Debug.Log($"[NpcInjectionZone] Player left NPC ENGAGEMENT zone (injections allowed)");
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
