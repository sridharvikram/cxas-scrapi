# Guide: Merging Voice and Chat CXAS Agents into One Polymorphic App

Reference accompanying `SKILL.md` for **`cxas-omnichannel-merger`**. Synthesizes
the official CES template-engine contract, the CXAS polymorphic-agents guide,
and the CXAS Best Practices Guide.

## 1. Template Delimiters: Static vs. Dynamic Wire Scoping

CES runs **one** unified template engine across every agent instruction
(`instruction.txt` and `global_instruction.txt`). Delimiter choice determines
both **what renders in `system_instruction`** and **whether `<state_update>`
history turns are injected into `LlmRequest.contents`**:

| Pattern | Syntax | `system_instruction` | `<state_update>` in `contents`? | Recommended Use |
| :--- | :--- | :--- | :---: | :--- |
| **Static print** | `{{ var }}` | Replaced inline every step (`null` leaves literal `{{ var }}` unless `\| default("...", true)` is attached). | **No (`0 bytes`)** | Session-start profile values, filtered prints, `{{ current_date }}`, large swapped text. |
| **Dynamic print** | `{var}` | Kept literally as `{var}` (preserves KV prompt prefix cache!). | **Yes** | Compact mid-session scalar counters/flags (`{retry_count}`, `{order_status}`). |
| **Static block** | `{{% if cond %}}...{{% endif %}}` | Evaluates branch per step; drops inactive branches. | **No (`0 bytes`)** | **Default for omnichannel merging** and gating large payloads cleanly. |
| **Dynamic block** | `{% if cond %}...{% endif %}` | Evaluates branch per step; drops inactive branches. | **Yes** (root vars in `cond`) | Branching on compact scalar flags when history tracking is desired. |

**Important callback timing rule:** CES renders Jinja instructions **after**
`before_agent_callback` on Step 1, **again after every tool call**, and
**before** `before_model_callback`. Derive variables read by templates inside
`before_agent_callback` (or Python tools), **not** `before_model_callback`
(which causes a silent 1-turn lag).

## 2. Official Modality & Channel Block Tag Allowlist

Every `{@start...}` tag compiles into a **double-brace** conditional
(`{{% if ... %}}`), and every `{@end...}` tag compiles to **`{{% endif %}}`**.
Because every closer translates identically to `{{% endif %}}`, mismatched
closers like `{@startModality_Voice}...{@endModality_Text}` silently pass basic
save validation — `omnichannel_merger.py verify` catches them strictly.

### Modality Tags (`_session.modality`)

| Console / Friendly Tag | Uppercase Alias | Compiled Condition | Notes |
| :--- | :--- | :--- | :--- |
| `{@startModality_Text}` | `{@startModality_TEXT}` | `{{% if _session.modality == 'TEXT' %}}` | Chat / text turns only |
| `{@startModality_Voice}` | `{@startModality_VOICE}` | `{{% if _session.modality == 'VOICE' %}}` | Voice / audio turns only |

### Channel Tags (`_session.channel`)

| Category | Friendly Tag | Enum / Alias Value |
| :--- | :--- | :--- |
| Generic API | `{@startChannel_API}` | `API` |
| Web widget / Console | `{@startChannel_WebWidget}` / `{@startChannel_WEB_UI}` | `WEB_UI` |
| Chat (CCaaS) | `{@startChannel_GoogleCCaaSChat}` | `CONTACT_CENTER_AS_A_SERVICE_CHAT` |
| Chat (Messaging) | `{@startChannel_WhatsApp}` / `{@startChannel_Whatsapp}` | `WHATSAPP` |
| Chat (Messaging) | `{@startChannel_Instagram}` | `INSTAGRAM` |
| Voice (GTP) | `{@startChannel_GoogleTelephonyPlatform}` | `GOOGLE_TELEPHONY_PLATFORM` |
| Voice (CCaaS) | `{@startChannel_GoogleCCaaSVoice}` | `CONTACT_CENTER_AS_A_SERVICE` |
| Voice (Twilio) | `{@startChannel_Twilio}` | `TWILIO` |
| Voice (Five9) | `{@startChannel_Five9}` | `FIVE9` |
| Voice (AudioCodes) | `{@startChannel_AudioCodes}` | `CONTACT_CENTER_INTEGRATION` |

**Common Trap:** `{@startChannel_Telephony}` / `{@startChannelTELEPHONY}` is
**NOT** supported (`400 Bad Request` on save). Use `{@startModality_Voice}` for
all voice channels, or check specific enum values inside a Jinja list:

```jinja
{{% if _session.channel in ['GOOGLE_TELEPHONY_PLATFORM', 'TWILIO', 'FIVE9', 'CONTACT_CENTER_AS_A_SERVICE', 'CONTACT_CENTER_INTEGRATION'] %}}
If the caller enters digits on their phone keypad, capture keypad input cleanly.
{{% endif %}}
```

## 3. Reserved Platform Variables (`app.json`)

Do **not** add these keys to `app.json` → `variableDeclarations` (the platform
injects them automatically and rejects updates that declare them):

- **`_session`** (`_session.modality`, `_session.channel`, `_session.recording_control`)
- **`current_date`** (ISO 8601 date in the app's configured `timeZoneSettings.timeZone`)
- **`session_metadata`**

For custom variables:
- Use **`snake_case`** (`^[a-zA-Z_][a-zA-Z0-9_]*$`). Avoid hyphens (`-`) because
  the expression lexer rejects them inside filters and conditionals.
- Avoid ADK colon prefixes (`app:`, `user:`, `temp:`) in `variableDeclarations`.
- Keep total declared variables **≤ 95** (consolidate related fields into a
  structured `OBJECT` variable; note `NUMBER` deserializes as Python `float`, so
  use `STRING` for IDs, order numbers, and ZIP codes).

## 4. Keep Top-Level PIF XML Static

Always keep structural XML blocks unwrapped and place modality tags **inside**:

```xml
<!-- GOOD -->
<persona>
Warm, helpful, and concise.
{@startModality_Voice}
Speak in natural sentences without markdown or bullet lists. Read order numbers digit by digit.
{@endModality_Voice}
{@startModality_Text}
Use concise markdown formatting with bullet points and clickable links where helpful.
{@endModality_Text}
</persona>

<!-- BAD (wraps top-level XML tag) -->
{@startModality_Voice}
<persona>...</persona>
{@endModality_Voice}
```

## 5. Neutral Verbs for Shared Prose

Outside modality blocks, replace channel-biased verbs with modality-neutral
language when tightening merged sentences:

| Avoid in Shared Lines | Prefer Instead |
| :--- | :--- |
| `say`, `speak`, `read aloud`, `tell the caller` | `respond`, `inform`, `provide`, `share` |
| `display`, `show`, `render`, `print`, `click`, `tap` | `provide`, `present`, `guide the user to select` |
| `caller` / `chatter` | `customer` or `user` |

## 6. Isolating Tools, Widgets, and Callbacks

Prompt branching strips *prose* from `system_instruction`, **not** declared
tool schemas from `llm_request.config.tools`. Whenever a tool or widget applies
to only **one** modality:

1. Keep it inside the matching `{@startModality_*}` instruction block.
2. Hide it dynamically in a **single consolidated `before_model_callback`**:

```python
from typing import Optional

TEXT_ONLY_TOOLS = ("render_order_card",)
VOICE_ONLY_TOOLS = ("send_dtmf_prompt",)


def _hide_disallowed_modality_tools(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> None:
    session = callback_context.get_variable("_session", {})
    modality = (
        session.get("modality", "").upper()
        if isinstance(session, dict)
        else ""
    )
    config = getattr(llm_request, "config", None)
    if config is None or not modality:
        return
    excluded = set(getattr(config, "excluded_tools", None) or ())
    to_hide = VOICE_ONLY_TOOLS if modality == "TEXT" else (
        TEXT_ONLY_TOOLS if modality == "VOICE" else ()
    )
    for name in to_hide:
        if name not in excluded and hasattr(config, "hide_tool"):
            config.hide_tool(name)


def before_model_callback(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    _hide_disallowed_modality_tools(callback_context, llm_request)
    return None
```

3. For UI widgets referenced only in `TEXT` (`{@WIDGET: ...}`), also add an
   explicit voice guard inside `{@startModality_Voice}` (e.g., *"Never invoke or
   reference any visual widget on voice calls."*).
4. Keep **one-time initialization guarded** in `before_agent_callback` so Turn
   2+ does not reset conversation state:

```python
from typing import Optional


def before_agent_callback(
    callback_context: CallbackContext,
) -> Optional[Content]:
    state = callback_context.state
    if state.get("_initialized") == "true":
        return None
    state["_initialized"] = "true"
    # Run Turn-1 bootstrap here.
    return None
```

5. In `after_model_callback`, read model output via **`part.text_or_transcript()`**
   instead of `part.text` so inspection works across both voice and chat streams.

## 7. `gemini-composite-v2` Voice Tags & Dual Deployments

- Inline angle-bracket emotion/prosody tags (`<prosody rate="64%">`, `<warm>`,
  `<calm>`, `<empathetic>`, `<short pause>`, `<long pause>`) belong **only**
  inside **`{@startModality_Voice}...{@endModality_Voice}`** blocks (and never
  in `TEXT` chat blocks).
- Remap legacy tags (`</prosody>`, `<slow>`, `[warm]`) using
  `cxas-composite-voice-agent-optimizer`.
- Inside `global_instruction.txt`, remember **save-time validation does not
  compile Jinja blocks** — always run `omnichannel_merger.py verify --strict`
  (and `cxas lint`) before testing or deploying.
- Provision **two separate deployments** pointing at the same merged app:
  - One **`VOICE`** deployment (telephony / audio profile)
  - One **`TEXT`** deployment (web chat / messaging profile)
  Templates are written to `deployment_templates/` inside `--report-dir` (never
  auto-applied to remote servers).
