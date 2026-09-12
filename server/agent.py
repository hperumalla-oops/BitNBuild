"""
The tool-calling loop.

The browser sends the whole conversation every turn, so the model always has
the thread — that is what makes the chat persistent rather than a series of
one-shot questions. The server keeps no state.
"""

import json

from . import tools as T

CHAT_MODEL = "gpt-5.4-mini"     # same model the zonal_rag pipeline uses
MAX_TOOL_ROUNDS = 6             # a stuck loop costs money; cap it

SYSTEM = """You answer questions about Bengaluru civic and planning data.

You are in {mode} mode, so you see that dataset's tools — plus web_search,
which is available in BOTH modes.

Use web_search only for what NEITHER database covers: current news, recent
announcements, background on a place or project. Reaching for it is not a
reason to suggest switching modes.

If the question really belongs to the OTHER DATABASE — zoning regulation while
you are in map mode, or the ward map while you are in zoning mode — say so and
point at the toggle. Do NOT answer it from web_search instead. The other
database is the authoritative, citable source; a web answer would read as
equivalent while being unsourced and possibly wrong for Bengaluru.

Answer in English.

{scope}

How to answer:
- Lead with the answer, then the supporting numbers. Keep it short.
- Every figure must come from a tool call. Never estimate, never fill a gap
  from memory. If the tools do not have it, say so plainly.
- Cite what you used: a pdf_url for zoning, a ward or station name for the map.
- When a tool returns a map_link, include it as a markdown link so the user can
  jump to that place on the map, e.g. [Sunkenahalli](?tab=map&ward=1:42).
- Prefer per-area or per-capita figures over raw counts when comparing wards,
  and say which you used.

Data caveats you must respect rather than paper over:
- Only 4 of 369 wards have itemised civic reports; the rest carry headline
  counts only. Do not imply a full report list exists for a ward that has none.
- 18 wards are absent from the report dataset entirely. That is "no data", not
  zero reports — never report them as zero.
- 12 metro stations lie outside the ward layer, so they have no parent ward.
- Zoning prose comes from PDFs; some rows are flagged is_synthetic, meaning a
  model generated them as a fallback rather than reading them from a document.
  If you use a synthetic row, say so."""

SCOPE = {
    "zoning": """Zoning mode covers Bengaluru planning regulation: permitted
uses, FAR, setbacks, heights, parking, coverage, plot sizes and density, plus
2,629 chunks of regulation prose with page-level PDF links.

Use zoning_rules for any number. Use zoning_search for wording and definitions.
Vector search is poor at exact figures — do not read a number out of prose when
a rule table has it.""",
    "map": """Map mode covers the 369 GBA wards: civic report counts, civic
pressure, zoning class and height band per ward, population and area, elected
representatives, 126 metro stations and 16 road projects, with PostGIS for
spatial questions.

Ward keys look like '5:25' (corporation:ward). Users give names, so resolve
with ward_lookup first, then use the ward_key with the other tools.""",
}


def run(messages, mode, tools_impl, openai_client, on_event=None):
    """Run one turn. Returns (reply_text, tool_trace)."""
    mode = "zoning" if mode == "zoning" else "map"
    schemas = T.schemas_for(mode)
    allowed = T.names_for(mode)

    convo = [{"role": "system",
              "content": SYSTEM.format(mode=mode, scope=SCOPE[mode])}]
    for m in messages:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            convo.append({"role": m["role"], "content": m["content"]})

    trace = []
    for _ in range(MAX_TOOL_ROUNDS):
        resp = openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=convo,
            tools=schemas,
            max_completion_tokens=4096,
        )
        msg = resp.choices[0].message
        calls = msg.tool_calls or []

        if not calls:
            return (msg.content or "").strip(), trace

        convo.append({
            "role": "assistant",
            "content": msg.content or None,
            "tool_calls": [{
                "id": c.id, "type": "function",
                "function": {"name": c.function.name,
                             "arguments": c.function.arguments},
            } for c in calls],
        })

        for c in calls:
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}

            if name not in allowed:
                # hard scope: refuse rather than silently answer from the
                # other dataset
                result = {"error": f"{name} is not available in {mode} mode. "
                                   "Tell the user to switch modes."}
            else:
                fn = getattr(tools_impl, name, None)
                try:
                    result = fn(**args) if fn else {"error": f"no such tool {name}"}
                except TypeError as e:
                    result = {"error": f"bad arguments for {name}: {e}"}
                except Exception as e:                      # network, HTTP, SQL
                    result = {"error": f"{name} failed: {type(e).__name__}: {e}"}

            trace.append({"tool": name, "args": args,
                          "ok": "error" not in (result or {})})
            if on_event:
                on_event({"type": "tool", "name": name, "args": args})

            convo.append({"role": "tool", "tool_call_id": c.id,
                          "content": json.dumps(result, default=str)[:60000]})

    return ("I kept needing more lookups and hit the tool limit for this turn. "
            "Try narrowing the question."), trace
