# Personas

Character voices for the three speech roles. Lighter-weight than the
`prompts/` + `prompts.local/` mechanism — one config line swaps the
voice across every provider you've configured.

## Layout

```
personas/                ← shipped defaults (this directory).
  marvin.md              the default character for monologue and notification

personas.local/          ← your overrides, gitignored. Same flat layout, no
                          provider axis.
```

## How resolution works

Each role in `config.json`'s `personas` block resolves like this:

```
"personas": {
  "monologue": "marvin",                       ← file lookup: personas[.local]/marvin.md
  "notification": "a panto dame",              ← no file matches → used verbatim
  "main": null                                 ← no persona override (default)
}
```

The rule is deliberately dumb: if `personas.local/<value>.md` or
`personas/<value>.md` exists, load it; otherwise treat the value as a
freeform character description and pass it through as-is.

## Adding a persona

Drop a markdown file in `personas.local/` (gitignored, survives
`git pull`) or `personas/` (shipped, gets committed). The body should
read as a **noun phrase** describing the character — it slots into the
sentence "in the voice of: …" in the prompt templates.

```markdown
<!-- The leading comment block (optional) is stripped at load time. -->

a wildly excited pantomime dame: theatrical, breathless, prone to addressing the audience as "darlings"
```

Keep it short — one or two sentences. No trailing punctuation. The
character should be discoverable in a single read.

## Three roles, two verbs

- `monologue` and `notification` — the LLM **adopts** the character.
  The persona text is slotted into each provider's `preamble.md` /
  `notification.md` via the `{persona}` placeholder.
- `main` — the summariser **preserves** a voice already in the reply
  (something the upstream agent put there). When set, the provider's
  `reformat_text()` appends one sentence to the summariser system
  prompt at runtime asking it to preserve a beat of that voice.
  Defaults to `null` — no append, no behaviour change.

## Provider-specific knobs that personas don't touch

- **OpenAI's `voices.openai.<role>.instructions`** controls the *TTS
  delivery* (the gravelly weariness in the actual audio), separately
  from the LLM prompt. Swapping `personas.monologue: "panto-dame"`
  while leaving the default Marvin TTS instructions in place produces
  a dame's *words* in Marvin's *voice* — not always what you want.
  Update both for a clean swap.
- **The Mistral classifier** is persona-neutral and picks from nine
  fixed emotional styles. A non-Marvin persona may sound tonally off
  if the classifier picks `sad` or `shameful`; treat as a follow-up if
  it bites.
