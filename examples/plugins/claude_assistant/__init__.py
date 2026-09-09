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

from datetime import datetime, timezone

PLUGIN = {
    "name": "claude-assistant",
    "version": "1.0.0",
    "description": "Ask Claude about the open case — schema-aware help writing SQL pane queries and interpreting artifacts. Needs network + ANTHROPIC_API_KEY.",
}

WINNOW_API_VERSION = 6   # req.table (plugin-owned tables in the case file)

SYSTEM_PROMPT = """You are a DFIR analyst's assistant embedded in Winnow, \
a local SQLite-backed tool for triaging forensic CSV/EVTX/registry exports.

The analyst can run read-only SQL in Winnow's SQL pane. When a question is \
best answered with a query, write SQLite SQL against the src_N tables in \
the provided schema and say what the result will show. Every column is \
stored as TEXT regardless of the type noted in schema comments — cast \
explicitly (CAST(col AS INTEGER)) for numeric comparisons and remember \
timestamps are strings. Keep answers focused and practical; when you \
reference an artifact or event ID, say why it matters for the investigation."""


# The transcript lives in the CASE FILE, in this plugin's own table, so it
# renders when the service is unreachable and travels with the .db when the
# case is handed to another analyst. It is not a source: it never appears in
# the grid or a merge. See docs/writing-plugins.md, "Your own tables".
HISTORY_COLUMNS = "id INTEGER PRIMARY KEY, role TEXT, content TEXT, at TEXT"


def _history(req):
    """This case's transcript table, or None with no case open (the plugin
    still answers questions then — it just has nothing to remember with)."""
    if req.store is None:
        return None
    return req.table("history").create(HISTORY_COLUMNS)


def _turns(req, limit=None):
    t = _history(req)
    return t.rows("ORDER BY id", limit=limit) if t else []


def history(req):
    """GET /api/plugin/claude_assistant/history -> {turns: [{role, content, at}]}
    What the tab renders on mount, with no network involved."""
    return {"turns": [{"role": r["role"], "content": r["content"], "at": r["at"]}
                      for r in _turns(req)],
            "persisted": req.store is not None}


def clear(req):
    """POST /api/plugin/claude_assistant/clear — forget this case's chat."""
    t = _history(req)
    if t:
        t.execute("DELETE FROM {table}")
    return {"ok": True}


def register(api):
    api.register_tab(
        id="chat",
        label="Claude",
        entry="ui/tab.js",
        description="Ask Claude about the open case — it sees the table schemas (not the data) and writes SQL-pane queries.",
    )
    api.register_api("ask", ask, methods=["POST"])
    api.register_api("history", history, methods=["GET"])
    api.register_api("clear", clear, methods=["POST"])


def ask(req):
    """POST /api/plugin/claude_assistant/ask
    body: {question, schema: str|null}
    -> {answer, model, stop_reason, usage}

    Context comes from this case's stored transcript, not from the browser:
    the tab can be closed, reopened or reloaded and the conversation carries
    on where it left off.
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
    for turn in _turns(req)[-MAX_HISTORY:]:
        role, content = turn.get("role"), turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    # A key the analyst saved under Settings → Environment wins; otherwise
    # the SDK resolves ANTHROPIC_API_KEY / `ant auth login` as it always
    # has. req.env only reads WINNOW_* names, which is why the Winnow-side
    # one is spelled that way.
    key = req.env("WINNOW_ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
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
            "No Anthropic credentials — save WINNOW_ANTHROPIC_API_KEY under "
            "Settings → Environment (or set ANTHROPIC_API_KEY / run `ant auth login` "
            "in the environment Winnow's server runs in, then restart it)"
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

    # Both turns land together, after the call succeeded: a question that
    # errored is shown in the tab but never becomes context for the next
    # one. SQLite serialises the write, so two tabs asking at once cannot
    # interleave a pair.
    t = _history(req)
    if t:
        at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        t.insert([{"role": "user", "content": question, "at": at},
                  {"role": "assistant", "content": answer, "at": at}])

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
