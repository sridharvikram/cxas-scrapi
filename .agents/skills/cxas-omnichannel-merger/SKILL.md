---
name: cxas-omnichannel-merger
description: >-
  Audits, merges, and verifies separate sibling Voice (IVR) and Chat (IVA) CXAS
  (CES / GECX) agent exports into a single polymorphic omnichannel app using
  minimal modular tag pairs ({@startModality_Voice}/{@startModality_Text} or
  double-brace {{% if _session.modality == ... %}}). Works on plain, non-Jinja
  instructions as well as templated prompts. Surfaces policy drift, variable
  schema conflicts, brace mismatches, and CES Jinja gotchas; scaffolds a single
  before_model_callback that masks modality-only tools via config.hide_tool();
  and proves render equivalence for both VOICE and TEXT offline without pushing
  to any server. Use when combining separate Voice and Chat apps, adding Voice
  to an existing Chat agent (or vice versa), or checking cross-channel policy
  drift. Don't use for greenfield agent scaffolding (use cxas-agent-foundry),
  single-agent progressive-disclosure routing (use cxas-self-steering-agent),
  Dialogflow CX migration (use cxas-dfcx-migration), or voice prosody/emotion
  tuning alone (use cxas-composite-voice-agent-optimizer).
---

# CXAS Omnichannel Merger (Voice + Chat)

Combines a sibling **Voice** CXAS app and **Chat** CXAS app into **one unified
polymorphic app** backed by a deterministic Python CLI (`audit`, `merge`,
`verify`). Neither source app needs pre-existing Jinja blocks or single/double
brace tags — plain XML instructions work out of the box.

Grounded in the CXAS Modular Components templating specification and the CXAS
Best Practices Guide (PIF XML structure, `gemini-composite-v2` audio tag
isolation, single consolidated `before_model_callback`, turn guards in
`before_agent_callback`, and zero-`<state_update>` double-brace blocks).

## Safety First

- **`audit` and `verify` are strictly read-only** and safe to run anytime.
- **`merge` writes only to a brand-new `--output-dir`**; it never mutates your
  source Voice or Chat exports in place.
- **Never run `cxas push`, `import_app`, or `update_agent` automatically.**
  Show the drift report, unified diff, and verification summary to the human
  engineer and wait for explicit approval before deploying anything.
- **Never write scratch or report artifacts inside a tracked app root** used
  by `cxas push` — place reports in `/tmp/` or a separate `--report-dir`.

## Quick Start

```bash
# 1. Read-only audit & drift report between sibling Voice and Chat exports
python3 .agents/skills/cxas-omnichannel-merger/scripts/omnichannel_merger.py audit \
  --voice-dir <path/to/voice_app> \
  --chat-dir <path/to/chat_app> \
  --report-md /tmp/drift_report.md \
  --report-json /tmp/drift_report.json

# 2. Lossless merge into a NEW directory (default: native {@startModality_*} tags)
python3 .agents/skills/cxas-omnichannel-merger/scripts/omnichannel_merger.py merge \
  --voice-dir <path/to/voice_app> \
  --chat-dir <path/to/chat_app> \
  --output-dir /tmp/merged_app \
  --report-dir /tmp/merged_reports \
  --tag-style ces-tags

# Optional: use explicit double-brace Jinja blocks instead of {@startModality_*}
python3 .agents/skills/cxas-omnichannel-merger/scripts/omnichannel_merger.py merge \
  --voice-dir <path/to/voice_app> \
  --chat-dir <path/to/chat_app> \
  --output-dir /tmp/merged_app \
  --tag-style jinja

# 3. Verify lossless VOICE & TEXT render equivalence + tag/Jinja/TTS hygiene
python3 .agents/skills/cxas-omnichannel-merger/scripts/omnichannel_merger.py verify \
  --merged-dir /tmp/merged_app \
  --voice-dir <path/to/voice_app> \
  --chat-dir <path/to/chat_app> \
  --strict
```

If directory names differ between sibling agents, map them explicitly:

```bash
--agent-map voice_orders=chat_order_agent --agent-map voice_root=chat_root
```

Try it immediately on the bundled sample pair (`resources/sample-pair/`):

```bash
python3 .agents/skills/cxas-omnichannel-merger/scripts/omnichannel_merger.py merge \
  --voice-dir .agents/skills/cxas-omnichannel-merger/resources/sample-pair/voice_app \
  --chat-dir .agents/skills/cxas-omnichannel-merger/resources/sample-pair/chat_app \
  --output-dir /tmp/sample_merged \
  --report-dir /tmp/sample_reports
```

## 4-Gate Workflow

1. **Gate 1 — Baseline Audit (`audit`):** Pair agents, tools, callbacks,
   `variableDeclarations`, `instruction.txt`, and `global_instruction.txt`.
   Group every finding into **auto-mergeable** vs. **`NEEDS_APPROVAL`**. If the
   user only asked for a drift or readiness check, stop here and share the report.
2. **Gate 2 — Deterministic Lossless Merge (`merge`):** Write a new merged app
   under `--output-dir` keeping top-level PIF XML tags (`<role>`, `<persona>`,
   `<primary_goal>`, `<constraints>`, `<error_protocol>`, `<taskflow>`,
   `<examples>`) **static** and wrapping **only** diverging lines inside
   `{@startModality_Voice}` / `{@startModality_Text}` (or double-brace
   `{{% if _session.modality == ... %}}`). Scaffold modality tool masking inside
   a **single consolidated `before_model_callback`** when `voice_only` or
   `text_only` tools exist, and emit dual deployment templates
   (`deployment_voice.template.json` / `deployment_text.template.json`) under
   `--report-dir`.
3. **Gate 3 — Human Approval & Tightening (`NEEDS_APPROVAL`):** Walk the user
   through every flagged item before resolving:
   - Policy / numeric drift (e.g., `30 days` in Voice vs. `45 days` in Chat)
   - `variableDeclarations` schema type conflicts (`STRING` vs. `NUMBER`)
   - Same-name tool or callback code diffs
   - Variable brace drift (`{order_id}` in Voice vs. `{{order_id}}` in Chat, or
     legacy `${var}`)
   - Optional clause-level refactoring: replace channel-biased verbs outside
     modality blocks (`say`/`speak`/`display`/`show`/`click`) with **neutral
     verbs** (`respond`, `inform`, `provide`, `share`), isolating tags to the
     smallest diverging phrase.
4. **Gate 4 — Offline Equivalence & Linter Gate (`verify` + `cxas lint`):**
   Confirm `verify --strict` reports zero tag/Jinja/TTS/leakage defects and
   run `cxas lint --app-dir <merged_dir>` before proposing any patch.

## Allowed vs. Disallowed Rules

Read [references/guide.md](references/guide.md) for full details and tag
mappings. Follow these core rules whenever merging or tightening prompts:

### Auto-Allowed (`SAFE_AUTO`)

- **Shared identical lines** remain unwrapped once outside modality blocks.
- **Diverging modality clauses** are wrapped **inside** static top-level XML
  sections using `{@startModality_Voice}...{@endModality_Voice}` and
  `{@startModality_Text}...{@endModality_Text}` (compiles to double-brace
  `{{% if _session.modality == '...' %}}` with **`0 bytes`** of `<state_update>`
  history injection).
- **Disjoint tools & variables** are unioned into the merged app, with
  `voice_only` / `text_only` tools automatically hidden via
  `llm_request.config.hide_tool(...)` using
  `callback_context.get_variable("_session", {})`.
- **`gemini-composite-v2` audio tags** (`<warm>`, `<calm>`, `<empathetic>`,
  `<prosody rate="...">`, `<short pause>`, `<long pause>`) stay isolated
  **inside `VOICE` blocks only** so they never leak into Chat text responses.

### Disallowed / Blocked (`NEEDS_APPROVAL`)

- **Never wrap top-level XML tags** (`<role>`, `<persona>`, `<constraints>`,
  `<taskflow>`, `<examples>`, `<goal>`) inside `{@startModality_*}` or
  `{@startChannel_*}`.
- **Never declare reserved platform variables** (`_session`, `current_date`,
  `session_metadata`) in `app.json` `variableDeclarations` — the server rejects
  them with `400 Bad Request`.
- **Never emit unsupported CES Jinja constructs:**
  - Unsupported filters (`| lower`, `| trim`, `| title`, `| join`, etc.) — only
    **`length`**, **`upper`**, **`default`** are supported.
  - Callable `upper(x)` — `upper` is a pipe filter only (`{{ x | upper }}`).
  - Keyword arguments (`| default("x", boolean_eval=true)`) — positional only:
    `{{ x | default("x", true) }}`.
  - Unsupported `loop.*` attributes (`loop.index`, `loop.first`) — only
    **`loop.last`** is supported.
  - Operators/syntax: `not in`, `dict['key']` subscripts, `{# ... #}` /
    `{{# ... #}}` comments, `{% set %}`, nested `{var}` inside `{% ... %}`.
  - Negative numeric literals in expressions (`{% if delta > -10 %}`) — compare
    against `0` or a session variable instead.
  - Hyphenated variable names in expressions/filters (`{% if caller-id %}` or
    `{{ caller-id | upper }}`) — use `snake_case` (`caller_id`).
  - Colon-prefixed `variableDeclarations` (`app:`, `user:`, `temp:`).
  - Invalid channel tag `{@startChannel_Telephony}` (use `{@startModality_Voice}`
    or specific carrier channel tags in [references/guide.md](references/guide.md)).
- **Never blindly pick a winner** on policy/numeric drift, `variableDeclarations`
  type conflicts, variable brace mismatches (`{var}` vs. `{{var}}`), or
  diverging Python code with the same tool/callback name.
- **Avoid ending prompt lines with raw closing braces (`}}`)** on lines outside
  tags when rendering literal templates, and guard `global_instruction.txt`
  strictly (the server skips save-time Jinja syntax validation on
  `global_instruction.txt` — one broken tag silently disables templating for the
  entire global instruction!).
- **Stay under the 95-variable ceiling** (`app.json` `variableDeclarations`) and
  **never split callbacks unnecessarily** across extra files (each callback file
  incurs per-invocation AST compilation overhead).
- **Guard one-time `before_agent_callback` resets** (`if state.get("_initialized") == "true": return None`, `if not state.get("_initialized"):`, or `.setdefault(...)`) so Turn 2+ does not wipe session state.
- **Add explicit `from typing import Any, Optional`** at the top of generated
  callbacks, and use **`part.text_or_transcript()`** (not `part.text`) inside
  `after_model_callback` when reading mixed voice/text responses.
