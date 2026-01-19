using UnityEngine;

/// <summary>
/// Paul Adams NPC character.
/// Inherits from LiveLlmCharacterBase for WebSocket communication and TTS.
/// </summary>
public class PaulAdamsCharacter : LiveLlmCharacterBase
{
    protected override void Awake()
    {
        characterId = "PaulAdams";
        base.Awake();
    }
}
