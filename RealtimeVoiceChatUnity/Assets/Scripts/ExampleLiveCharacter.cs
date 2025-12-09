using UnityEngine;

public class Example_LiveLLM : LiveLlmCharacterBase
{
    protected override void Awake()
    {
        characterId = "PaulAdams";
        base.Awake();
    }
}