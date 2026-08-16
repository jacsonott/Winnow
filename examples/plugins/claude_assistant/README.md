# claude-assistant — an LLM-integration tab plugin for Winnow

Adds a pinned **Claude** tab: a chat pane where the analyst asks
questions about the open case. With "send schema" checked (the default),
each question carries the case's table and column names — the same
LLM-ready schema dump the SQL pane's "Copy schema" button builds — so
Claude can write ready-to-paste SQL pane queries, suggest pivots, and
explain artifacts in context. **Row data is never sent** unless the
analyst pastes it into a question themselves.

## ⚠️ Not airgap-compatible — by design

This plugin calls the Anthropic API: it needs network access and
credentials, which is exactly why it ships as an opt-in plugin rather
than a Winnow feature. Airgapped machines never load a line of it.

## Install

1. `pip install -U anthropic` in the environment Winnow's server runs in
   (a plugin declares its own dependencies; Winnow's core stays stdlib+
   FastAPI only).
2. Credentials: `export ANTHROPIC_API_KEY=...` or `ant auth login`.
3. **Settings → Plugins → "Install a plugin folder…"** and pick this
   folder, or `cp -r examples/plugins/claude_assistant plugins/`.

## What it does on the API

- Model: `claude-opus-5`, streamed server-side (`get_final_message()`),
  adaptive thinking (the model's default).
- **Server-side refusal fallbacks are enabled** (`fallbacks: "default"`):
  DFIR questions live near the cybersecurity topics Claude's safety
  classifiers watch, and this re-runs a declined request on Anthropic's
  recommended substitute model in the same call instead of dead-ending
  the analyst. Remove the `fallbacks`/`betas` lines in `__init__.py` if
  you'd rather see raw refusals.
- The schema block carries a prompt-cache breakpoint, so repeated
  questions against the same case re-read the cached schema (~10× cheaper)
  instead of re-paying for it.
- A refusal that survives the fallback chain surfaces as an inline error
  in the chat, with the category when the API provides one.

## As a reference for writing your own plugin

Shows the same `register_tab` + `register_api` hooks as the
lateral-movement example, plus: calling an external service from a plugin
backend, `winnow.schemaText()` as UI-side context, per-conversation
history kept in the tab module, and inline error rendering.
