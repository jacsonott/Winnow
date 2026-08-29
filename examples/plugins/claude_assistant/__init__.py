"""Claude assistant plugin for Winnow.

A pinned "Claude" tab where the analyst can ask questions about the open
case — with the case *schema* (never row data, unless the analyst pastes
it into a question themselves) as context, so Claude can write queries
for the SQL pane, suggest pivots, or explain an artifact.

⚠️ This plugin talks to the Anthropic API — it needs network access and
credentials, so unlike everything else in Winnow it is NOT airgap-
compatible. That's exactly why it's a plugin: connected analysis machines
opt in by installing it; airgapped ones never load a line of it.

Requirements (documented, not vendored — a plugin declares its own deps):
  pip install -U anthropic
  export ANTHROPIC_API_KEY=...   # or `ant auth login` — the SDK resolves either

Server-side refusal fallbacks are enabled by default (`fallbacks:
"default"`): DFIR questions live near the cybersecurity topics Claude
Opus 5's safety classifiers watch, and the fallback re-runs a declined
request on Anthropic's recommended substitute model server-side instead
of surfacing a dead end to the analyst.
"""

MODEL = "claude-opus-5"
MAX_HISTORY = 40         # turns kept per request — the tab is a scratchpad, not an archive
MAX_TOKENS = 64000       # streamed, so a large ceiling is safe (no HTTP timeout risk)

PLUGIN = {
    "name": "claude-assistant",
    "version": "1.0.0",
    "description": "Ask Claude about the open case — schema-aware help writing SQL pane queries and interpreting artifacts. Needs network + ANTHROPIC_API_KEY.",
}

WINNOW_API_VERSION = 1

SYSTEM_PROMPT = """You are a DFIR analyst's assistant embedded in Winnow, \
a local SQLite-backed tool for triaging forensic CSV/EVTX/registry exports.

The analyst can run read-only SQL in Winnow's SQL pane. When a question is \
best answered with a query, write SQLite SQL against the src_N tables in \
the provided schema and say what the result will show. Every column is \
stored as TEXT regardless of the type noted in schema comments — cast \
explicitly (CAST(col AS INTEGER)) for numeric comparisons and remember \
timestamps are strings. Keep answers focused and practical; when you \
reference an artifact or event ID, say why it matters for the investigation."""


def register(api):
    api.register_tab(
        id="chat",
        label="Claude",
        entry="ui/tab.js",
        description="Ask Claude about the open case — it sees the table schemas (not the data) and writes SQL-pane queries.",
    )
    api.register_api("ask", ask, methods=["POST"])


def ask(req):
    """POST /api/plugin/claude_assistant/ask
    body: {question, history: [{role, content}], schema: str|null}
    -> {answer, model, stop_reason, usage}
    """
    b = req.body or {}
    question = (b.get("question") or "").strip()
    if not question:
        raise ValueError("Ask something")

    try:
        import anthropic
    except ImportError:
        raise ValueError(
            "The claude-assistant plugin needs the official SDK on the server: pip install -U anthropic"
        )

    # System prompt is [stable text, schema] with the cache breakpoint on
    # the schema block: the whole prefix is cached between questions and
    # only invalidates when the case's tables actually change.
    system = [{"type": "text", "text": SYSTEM_PROMPT}]
    schema = (b.get("schema") or "").strip()
    if schema:
        system.append({
            "type": "text",
            "text": "Current case schema:\n\n" + schema,
            "cache_control": {"type": "ephemeral"},
        })

    messages = []
    for turn in (b.get("history") or [])[-MAX_HISTORY:]:
        role, content = turn.get("role"), turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / `ant auth login` profile
    try:
        # Streamed with get_final_message(): the browser round trip stays a
        # plain JSON response, but the SDK connection can't hit its HTTP
        # timeout on a long answer. Thinking is deliberately not configured:
        # on claude-opus-5 omitting it runs adaptive thinking, which is the
        # recommended setting.
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # rescue policy declines on the recommended substitute, server-side
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ValueError(
            "No Anthropic credentials — set ANTHROPIC_API_KEY (or run `ant auth login`) "
            "in the environment Winnow's server runs in, then restart it"
        )
    except anthropic.APIConnectionError:
        raise ValueError("Could not reach the Anthropic API — this plugin needs network access")
    except anthropic.APIStatusError as e:
        raise ValueError(f"Anthropic API error ({e.status_code}): {getattr(e, 'message', e)}")

    if msg.stop_reason == "refusal":
        # The whole fallback chain declined. stop_details is informational
        # and can be None — never branch on it, but surface what's there.
        detail = getattr(msg, "stop_details", None)
        why = getattr(detail, "explanation", None) or getattr(detail, "category", None) or "safety policy"
        raise ValueError(f"Claude declined this request ({why}) — rephrase and try again")

    answer = "".join(block.text for block in msg.content if block.type == "text")
    return {
        "answer": answer,
        "model": msg.model,
        "stop_reason": msg.stop_reason,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        },
    }
