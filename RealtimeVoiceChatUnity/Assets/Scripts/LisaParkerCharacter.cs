using UnityEngine;

/// <summary>
/// Lisa Parker NPC character.
/// Inherits from LiveLlmCharacterBase for WebSocket communication and TTS.
/// </summary>
public class LisaParkerCharacter : LiveLlmCharacterBase
{
    protected override void Awake()
    {
        characterId = "LisaParker";
        base.Awake();
    }
}
