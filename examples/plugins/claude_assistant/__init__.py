"""Claude assistant plugin for Winnow.

A pinned "Claude" tab where the analyst can ask questions about the open
case — with the case *schema* (never row data, unless the analyst pastes
it into a question themselves) as context, so Claude can write queries
for the SQL pane, suggest pivots, or explain an artifact.

And a **Copilot** panel beside the SQL pane itself (register_page_panel):
the same chat, told to answer with one SQL block, with the editor's
current query as context — and each answer carries Insert / Run buttons
wired to winnow.sqlPage. Two conversations, two tables in the case file.

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
    "description": "Ask Claude about the open case — schema-aware help writing SQL pane queries and interpreting artifacts, plus a Copilot beside the SQL pane. Needs network + ANTHROPIC_API_KEY.",
}

WINNOW_API_VERSION = 9   # register_page_panel + winnow.sqlPage (the Copilot)

SYSTEM_PROMPT = """You are a DFIR analyst's assistant embedded in Winnow, \
a local SQLite-backed tool for triaging forensic CSV/EVTX/registry exports.

The analyst can run read-only SQL in Winnow's SQL pane. When a question is \
best answered with a query, write SQLite SQL against the src_N tables in \
the provided schema and say what the result will show. Every column is \
stored as TEXT regardless of the type noted in schema comments — cast \
explicitly (CAST(col AS INTEGER)) for numeric comparisons and remember \
timestamps are strings. Keep answers focused and practical; when you \
reference an artifact or event ID, say why it matters for the investigation."""

# The Copilot beside the SQL pane: same model, same schema, a narrower job.
# Its answers are inserted and run by buttons, so the *block* is a real
# contract — exactly one fenced sql block the UI can lift out, and no block
# at all when there is no query to give. The prose around it is not a
# contract: ui/copilot.js renders it as plain divs and truncates nothing,
# so the prompt asks for what the analyst needs to read rather than for a
# sentence count. The count this used to carry bought neatness at the price
# of the caveats that decide whether a result can be trusted — that a
# timestamp comparison is text-wise, say, and comes apart across exports
# that format their dates differently.
#
# The schema the UI sends is source tables only (static/js/plugins.js drops
# anything with is_merge, and the Copilot has no toggle to turn that off),
# but a merge is still queryable: the pane's connection carries a merge_<id>
# TEMP VIEW, and a selected merge puts `SELECT * FROM merge_N` in the editor
# text that rides along as context. So the prompt has to scope the schema
# claim rather than list merges as part of it — otherwise a literal reader
# "fixes" a perfectly good merge_3 query because merge_3 is not in the dump.
COPILOT_PROMPT = """You are the SQL copilot inside Winnow's SQL pane, helping a DFIR \
analyst query a case. The case is SQLite; the provided schema lists the \
case's src_N tables. A merged table is queryable as merge_N as well, even \
though the schema does not list its columns — if the analyst is working \
against one, write the query against merge_N rather than steering back to \
the sources. Every column is stored as TEXT whatever the schema comments \
say — CAST explicitly for numeric comparisons, and timestamps are \
ISO-like strings compared as text.

Answer with exactly one fenced ```sql block holding a single read-only \
SELECT, then tell the analyst what the result shows and anything that \
would make them read it wrong — a comparison that is text-wise, a column \
that is not filled in on every export, a filter narrower than it looks. \
Add LIMIT 200 unless the analyst asks for everything or an aggregate. If \
the analyst's message is about the query currently in the editor ("make \
this faster", "add the host", "why no rows?"), answer about that query \
and return the revised one. If a question cannot be answered with a \
query, say so and why, and give no code block."""

MODES = ("chat", "sql")   # the tab's conversation and the Copilot's — separate tables


# The transcript lives in the CASE FILE, in this plugin's own table, so it
# renders when the service is unreachable and travels with the .db when the
# case is handed to another analyst. It is not a source: it never appears in
# the grid or a merge. See docs/writing-plugins.md, "Your own tables".
HISTORY_COLUMNS = "id INTEGER PRIMARY KEY, role TEXT, content TEXT, at TEXT"


def _mode(value):
    mode = (value or "chat").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    return mode


def _history(req, mode="chat"):
    """This case's transcript table for one conversation — "history" for
    the tab, "copilot" for the SQL Copilot — or None with no case open
    (the plugin still answers questions then — it just has nothing to
    remember with)."""
    if req.store is None:
        return None
    return req.table("history" if mode == "chat" else "copilot").create(HISTORY_COLUMNS)


def _turns(req, mode="chat", limit=None):
    t = _history(req, mode)
    return t.rows("ORDER BY id", limit=limit) if t else []


def history(req):
    """GET /api/plugin/claude_assistant/history?mode=chat|sql
    -> {turns: [{role, content, at}], persisted}
    What the tab (or the Copilot) renders on mount, with no network involved."""
    mode = _mode((req.query or {}).get("mode"))
    return {"turns": [{"role": r["role"], "content": r["content"], "at": r["at"]}
                      for r in _turns(req, mode)],
            "persisted": req.store is not None}


def clear(req):
    """POST /api/plugin/claude_assistant/clear {mode} — forget one of this
    case's conversations."""
    t = _history(req, _mode((req.body or {}).get("mode")))
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
    api.register_page_panel(
        page="sql",
        id="copilot",
        label="Copilot",
        entry="ui/copilot.js",
        description="Ask Claude for a query — it sees the schema and the editor's current text, and its answers insert or run right here.",
    )
    api.register_api("ask", ask, methods=["POST"])
    api.register_api("history", history, methods=["GET"])
    api.register_api("clear", clear, methods=["POST"])


def ask(req):
    """POST /api/plugin/claude_assistant/ask
    body: {question, schema: str|null, mode?: "chat"|"sql", current_sql?: str}
    -> {answer, model, stop_reason, usage}

    Context comes from this case's stored transcript, not from the browser:
    the tab can be closed, reopened or reloaded and the conversation carries
    on where it left off. mode "sql" is the Copilot: its own prompt, its
    own transcript, and the editor's current query folded into the turn.
    """
    b = req.body or {}
    question = (b.get("question") or "").strip()
    if not question:
        raise ValueError("Ask something")
    mode = _mode(b.get("mode"))

    try:
        import anthropic
    except ImportError:
        raise ValueError(
            "The claude-assistant plugin needs the official SDK on the server: pip install -U anthropic"
        )

    # System prompt is [stable text, schema] with a cache breakpoint on
    # the schema block: the whole prefix is cached between questions and
    # only invalidates when the case's tables actually change.
    system = [{"type": "text", "text": COPILOT_PROMPT if mode == "sql" else SYSTEM_PROMPT}]
    schema = (b.get("schema") or "").strip()
    if schema:
        system.append({
            "type": "text",
            "text": "Current case schema:\n\n" + schema,
            "cache_control": {"type": "ephemeral"},
        })

    messages = []
    for turn in _turns(req, mode)[-MAX_HISTORY:]:
        role, content = turn.get("role"), turn.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    # The second cache breakpoint, on the last turn we replayed. The whole
    # transcript ahead of it is byte-identical from one question to the
    # next, and claude-opus-5 caches any prefix from 512 tokens up, so a
    # conversation of any substance is past break-even within a few
    # questions. Before this marker existed the schema was cached and up to
    # MAX_HISTORY turns of transcript behind it were re-read at full price
    # on every single question.
    #
    # It goes on the last *stored* turn and not on the one appended below.
    # In the Copilot the two are different text — what we send is the
    # question with the editor's SQL folded in, what the transcript keeps
    # is the question alone — so a marker there would write an entry keyed
    # on bytes no later request ever sends again. In the chat tab they
    # happen to be the same string, but the marker still belongs here: the
    # tail is the one turn that is unique to this request either way, and
    # a breakpoint is only worth its write premium on bytes that come back.
    # Top-level automatic caching is wrong for exactly that reason — it
    # places its breakpoint on the tail.
    #
    # The limitation worth stating plainly: once a conversation runs past
    # MAX_HISTORY turns the slice above starts moving, and it moves a PAIR
    # at a time — a successful ask stores the question and the answer
    # together — so the replayed prefix changes from its first byte and
    # this marker stops reading. The schema breakpoint in `system` is
    # unaffected (system renders before messages), so what is lost is the
    # transcript half, not the whole cache. Fixing it properly means a
    # window that advances in chunks big enough to leave a stable prefix
    # behind, rather than one that moves every request.
    if messages:
        tail = messages[-1]
        tail["content"] = [{"type": "text",
                            "text": tail["content"],
                            "cache_control": {"type": "ephemeral"}}]

    # The Copilot sees what is in the editor — "make this faster" needs
    # "this". It rides in the turn, not the transcript: the stored question
    # stays the analyst's words, and the next turn carries the editor's
    # text as it is then.
    current_sql = (b.get("current_sql") or "").strip() if mode == "sql" else ""
    sent = question + (f"\n\nThe query currently in the editor:\n```sql\n{current_sql}\n```" if current_sql else "")
    messages.append({"role": "user", "content": sent})

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
    t = _history(req, mode)
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
