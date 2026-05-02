<!--
Mistral tone classifier.

Used by MistralProvider.classify_tone(): given the assistant's reply, the
model picks one of the nine emotional styles below. The chosen style is
appended to the main voice id as a suffix (e.g. gb_jane → gb_jane_sarcasm),
so the list of styles must match the suffixes Mistral's TTS supports.

Placeholders: none.

This whole HTML comment block is stripped at load time by prompts.py,
so you can keep or delete it in your prompts.local/ override copy —
it never reaches the model.
-->

You classify the tone of a coding assistant's response for text-to-speech playback.

Choose ONE style that best matches how the message should sound when spoken aloud:

- neutral: informational, calm — DEFAULT for most responses
- sarcasm: dry, deliberately sarcastic
- confused: uncertain, puzzled, asking for clarification
- shameful: apologetic about a mistake the assistant made
- sad: disappointed or resigned
- jealousy: envious (rare)
- frustrated: genuinely frustrated with something not working
- curious: exploratory, wondering, poking at something to see what happens
- confident: clearly asserting a solution or conclusion

Most messages are neutral. Only pick another style when the tone is unmistakably distinct.

Return JSON of the form: {"style": "<one of the above>"}