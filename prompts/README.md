# Prompts

The system prompts each TTS provider sends to the LLM. One markdown file
per prompt, organised by provider.

## Layout

```
prompts/                 ← shipped defaults (this directory).
  mistral/
    classifier.md
    summary.md
    preamble.md
    notification.md
  xai/
    summary.md
    preamble.md
    notification.md
```

A user's overrides — if they have any — live in `prompts.local/`
alongside this directory, with the same `<provider>/<name>.md` layout.
That directory is gitignored. At runtime, `prompts.local/` wins;
otherwise the shipped file here is used.

## Editing a prompt

Don't edit files in this directory if you want your changes to survive
a `git pull` — copy them to `prompts.local/` first:

```bash
mkdir -p prompts.local/mistral
cp prompts/mistral/preamble.md prompts.local/mistral/preamble.md
$EDITOR prompts.local/mistral/preamble.md
```

The leading `<!-- ... -->` comment block in each file documents what
the prompt is for, which Provider method calls it, and what
`{placeholder}` substitutions (if any) are available. Anything in that
block is stripped at load time before the prompt reaches the model, so
leaving it in your override copy is harmless.

If your custom prompt accidentally contains a stray `{` (a JSON
example, say), the format step catches the error and falls back to the
unformatted template. Look for `<prompt format error>` in
`stop-hook.log` if a notification reads strangely.

## Adding prompts for a new provider

If you're writing a new provider in `providers/<name>.py`, ship the
matching defaults here under `prompts/<name>/`. See
[../providers/README.md](../providers/README.md) for the contract.
