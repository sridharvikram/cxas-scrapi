#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Offline CXAS / CES Voice + Chat omnichannel merger (`audit|merge|verify`).

Deterministically audits, merges, and verifies two sibling CXAS agent exports
(one Voice, one Chat) into a single polymorphic omnichannel app using either
native modality snippet tags (``{@startModality_Voice}`` /
``{@startModality_Text}``) or double-brace Jinja blocks
(``{{% if _session.modality == 'VOICE' %}}``).

Pure Python stdlib — zero remote API calls, zero modifications to source apps.
"""

import argparse
import ast
import difflib
import hashlib
import json
import re
import shutil
import sys
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ── CES Tag & Template Allowlists ────────────────────────────────────────

MODALITY_MAP: dict[str, str] = {
    "TEXT": "TEXT",
    "Text": "TEXT",
    "VOICE": "VOICE",
    "Voice": "VOICE",
}

CHANNEL_MAP: dict[str, str] = {
    "API": "API",
    "GoogleTelephonyPlatform": "GOOGLE_TELEPHONY_PLATFORM",
    "GOOGLE_TELEPHONY_PLATFORM": "GOOGLE_TELEPHONY_PLATFORM",
    "GoogleCCaaSVoice": "CONTACT_CENTER_AS_A_SERVICE",
    "CONTACT_CENTER_AS_A_SERVICE": "CONTACT_CENTER_AS_A_SERVICE",
    "Twilio": "TWILIO",
    "TWILIO": "TWILIO",
    "Five9": "FIVE9",
    "FIVE9": "FIVE9",
    "AudioCodes": "CONTACT_CENTER_INTEGRATION",
    "CONTACT_CENTER_INTEGRATION": "CONTACT_CENTER_INTEGRATION",
    "Whatsapp": "WHATSAPP",
    "WhatsApp": "WHATSAPP",
    "WHATSAPP": "WHATSAPP",
    "Instagram": "INSTAGRAM",
    "INSTAGRAM": "INSTAGRAM",
    "GoogleCCaaSChat": "CONTACT_CENTER_AS_A_SERVICE_CHAT",
    "CONTACT_CENTER_AS_A_SERVICE_CHAT": "CONTACT_CENTER_AS_A_SERVICE_CHAT",
    "WEB_UI": "WEB_UI",
    "WebWidget": "WEB_UI",
}

SUPPORTED_FILTERS: frozenset[str] = frozenset({"length", "upper", "default"})
SUPPORTED_LOOP_ATTRS: frozenset[str] = frozenset({"last"})
SUPPORTED_BLOCK_KEYWORDS: frozenset[str] = frozenset(
    {"if", "elif", "else", "endif", "for", "endfor", "macro", "endmacro"}
)
RESERVED_VARIABLES: frozenset[str] = frozenset(
    {"_session", "current_date", "session_metadata"}
)
VARIABLE_COUNT_LIMIT = 95

STRUCTURAL_XML_TAGS: frozenset[str] = frozenset(
    {
        "role",
        "persona",
        "primary_goal",
        "constraints",
        "error_protocol",
        "taskflow",
        "examples",
        "goal",
        "guidelines",
        "reply_shape",
        "ending_the_conversation",
    }
)
TOP_LEVEL_PROTECTED_TAGS: frozenset[str] = frozenset(
    {"role", "persona", "constraints", "taskflow", "examples", "goal"}
)

CALLBACK_DIRS: tuple[str, ...] = (
    "before_agent_callbacks",
    "before_model_callbacks",
    "after_model_callbacks",
    "before_tool_callbacks",
    "after_tool_callbacks",
    "after_agent_callbacks",
)

# ── Regex Patterns ───────────────────────────────────────────────────────

_EXEMPT_RE = re.compile(
    r"<\s*inline_example\b[^>]*>[\s\S]*?<\s*/\s*inline_example\s*>|"
    r"```[\s\S]*?```",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
_BLOCK_TAG_RE = re.compile(
    r"\{\{?%-?\s*((?:(?!-?%\}\}?)(?!\{\{?%).)*?)\s*-?%\}\}?",
    re.DOTALL,
)
_PRINT_TAG_RE = re.compile(r"\{\{(?![%@#])([^{}]*?)\}\}")
_SINGLE_VAR_RE = re.compile(
    r"(?<!\{)(?<!\$)\{([A-Za-z_][A-Za-z0-9_.-]*)\}(?!\})"
)
_DOUBLE_VAR_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)(?:\s*\|[^{}]*)?\s*\}\}"
)
_LEGACY_DOLLAR_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
_ANY_CES_TAG_RE = re.compile(
    r"\{@(start|end)(Modality|Channel)_?([A-Za-z0-9_]+)\}"
)
_COMMENT_RE = re.compile(r"\{\{?#[\s\S]*?#\}\}?")
_NOT_IN_RE = re.compile(r"\bnot\s+in\b")
_DICT_SUBSCRIPT_RE = re.compile(r"\b[A-Za-z_]\w*\[\s*['\"][^'\"]+['\"]\s*\]")
_FILTER_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")
_LOOP_ATTR_RE = re.compile(r"\bloop\.([A-Za-z_][A-Za-z0-9_]*)\b")
_HYPHEN_VAR_RE = re.compile(r"(?:^|[^\w.:/-])([A-Za-z_][\w.:]*(?:-[\w.:]+)+)")
_NEG_LITERAL_RE = re.compile(r"(?:==|!=|<=|>=|<|>)\s*-\s*\d+(?:\.\d+)?\b")
_KWARG_EQ_RE = re.compile(r"\([^{})]*\b[A-Za-z_]\w*\s*=(?!=)[^{})]*\)")
_UPPER_CALL_RE = re.compile(r"\bupper\s*\(")
_NESTED_BRACE_RE = re.compile(r"[{}]")
_TOOL_REF_RE = re.compile(r"\{@TOOL:\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}")
_WIDGET_REF_RE = re.compile(r"\{@WIDGET:\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}")
_TOP_XML_OPEN_RE = re.compile(
    r"<\s*(role|persona|constraints|taskflow|examples|goal)\b",
    re.IGNORECASE,
)

# Numeric/policy tokens for drift detection (dollars, percentages, days/hours).
_POLICY_VALUE_RE = re.compile(
    r"(?:\$\s*\d+(?:,\d{3})*(?:\.\d{1,2})?|"
    r"\b\d+(?:\.\d+)?\s*(?:%|percent|days?|hours?|minutes?|seconds?|"
    r"weeks?|months?|years?|attempts?|retries|times)\b)",
    re.IGNORECASE,
)

# Inline composite-v2 audio tags & legacy banned tags.
_COMPOSITE_AUDIO_TAG_RE = re.compile(
    r"<\s*(?:prosody\b[^>\n]*|warm|empathetic|calm|clear|positive|"
    r"sympathetic|enthusiasm|relief|interest|determination|sigh|sighs|"
    r"chuckles|exhales|whispers|whispering|short\s+pause|long\s+pause|"
    r"fast|faster)\s*>",
    re.IGNORECASE,
)
_LEGACY_TTS_TAG_RE = re.compile(
    r"</\s*prosody\s*>|<\s*(?:slow|slower|breath|laugh|gasp)\s*>|"
    r"\[(?:warm|calm|empathetic|slow|pause|whispers?|laughs?|sighs?)\]",
    re.IGNORECASE,
)

# Non-neutral verbs worth softening when sharing lines across modalities.
_BIASED_VOICE_VERB_RE = re.compile(
    r"\b(?:say|speak|read\s+aloud|tell\s+the\s+caller)\b", re.IGNORECASE
)
_BIASED_TEXT_VERB_RE = re.compile(
    r"\b(?:display|click|tap|markdown\s+link|clickable\s+link)\b",
    re.IGNORECASE,
)

_MODALITY_IF_RE = re.compile(
    r"^_session\.modality\s*(==|!=)\s*['\"](VOICE|TEXT)['\"]$",
    re.IGNORECASE,
)
_CHANNEL_IF_RE = re.compile(
    r"^_session\.channel\s*(==|!=)\s*['\"]([A-Z0-9_]+)['\"]$",
    re.IGNORECASE,
)
_XML_SECTION_RE = re.compile(
    r"^(?P<indent>[ \t]*)<(?P<tag>[A-Za-z_][A-Za-z0-9_-]*)"
    r"(?P<attrs>(?:\s+[^>]*)?)>(?P<rest>.*)$"
)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(https?://[^)]+\)")

_INIT_GUARD_TOKENS = (
    "init",
    "loaded",
    "called",
    "done",
    "once",
    "bootstrap",
    "started",
)


# ── Data Structures ──────────────────────────────────────────────────────


@dataclass
class Finding:
    """One audit, drift, or verification finding."""

    code: str
    severity: str  # "ERROR" | "WARNING" | "INFO"
    category: str  # "SAFE_AUTO" | "NEEDS_APPROVAL"
    location: str
    message: str
    fix: str = ""
    line: int | None = None


@dataclass
class AgentPair:
    """Matched or unilateral agent pair between Voice and Chat apps."""

    merged_name: str
    voice_name: str | None
    chat_name: str | None
    voice_dir: Path | None
    chat_dir: Path | None


@dataclass
class Section:
    """Top-level chunk of an instruction file (XML tag block or plain text)."""

    kind: str  # "xml" | "text"
    tag: str
    open_line: str
    close_line: str
    body_lines: list[str]
    key: str


@dataclass
class AuditReport:
    """Structured report produced by `audit` and `merge`."""

    voice_root: str
    chat_root: str
    agent_pairs: list[dict[str, str | None]] = field(default_factory=list)
    tools_shared: list[str] = field(default_factory=list)
    tools_voice_only: list[str] = field(default_factory=list)
    tools_text_only: list[str] = field(default_factory=list)
    tool_conflicts: list[str] = field(default_factory=list)
    variables_shared: list[str] = field(default_factory=list)
    variables_voice_only: list[str] = field(default_factory=list)
    variables_text_only: list[str] = field(default_factory=list)
    variable_conflicts: list[dict[str, str]] = field(default_factory=list)
    total_variables: int = 0
    findings: list[Finding] = field(default_factory=list)
    token_stats: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_root": self.voice_root,
            "chat_root": self.chat_root,
            "agent_pairs": self.agent_pairs,
            "tools": {
                "shared": self.tools_shared,
                "voice_only": self.tools_voice_only,
                "text_only": self.tools_text_only,
                "conflicts": self.tool_conflicts,
            },
            "variables": {
                "shared": self.variables_shared,
                "voice_only": self.variables_voice_only,
                "text_only": self.variables_text_only,
                "conflicts": self.variable_conflicts,
                "total": self.total_variables,
            },
            "token_stats": self.token_stats,
            "findings": [asdict(f) for f in self.findings],
        }


# ── Helpers ──────────────────────────────────────────────────────────────


def _approx_tokens(text: str) -> int:
    """Fast ~4 chars/token estimate (no external tokenizer required)."""
    return max(0, (len(text) + 3) // 4)


def _mask_exempt(text: str) -> str:
    """Replace `<inline_example>` and fenced code blocks keeping newlines."""

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _EXEMPT_RE.sub(_blank, text)


def _strip_quotes(expr: str) -> str:
    return _QUOTED_RE.sub(" '' ", expr)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _sha256_tree(directory: Path) -> dict[str, str]:
    """Return sorted `{rel_path: sha256}` ignoring `__pycache__`."""
    hashes: dict[str, str] = {}
    if not directory.is_dir():
        return hashes
    for item in sorted(directory.rglob("*")):
        if not item.is_file():
            continue
        if "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        rel = str(item.relative_to(directory))
        hashes[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
    return hashes


def resolve_app_root(path: Path) -> Path:
    """Resolve direct app root (`app.json`) or nested Foundry workspace root."""
    if not path.exists():
        raise FileNotFoundError(f"App path does not exist: {path}")
    if (path / "app.json").is_file() or (path / "agents").is_dir():
        return path
    candidates: list[Path] = []
    for sub in sorted(path.iterdir()):
        if not sub.is_dir() or sub.name.startswith("."):
            continue
        if (sub / "app.json").is_file():
            candidates.append(sub)
        elif sub.name == "cxas_app":
            for child in sorted(sub.iterdir()):
                if child.is_dir() and (child / "app.json").is_file():
                    candidates.append(child)
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError(
        f"Could not find unique app.json under {path} "
        f"(found {len(candidates)} candidate(s))."
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _discover_agents(app_root: Path) -> dict[str, Path]:
    agents_dir = app_root / "agents"
    if not agents_dir.is_dir():
        return {}
    return {d.name: d for d in sorted(agents_dir.iterdir()) if d.is_dir()}


def _discover_tools(app_root: Path) -> dict[str, Path]:
    tools_dir = app_root / "tools"
    if not tools_dir.is_dir():
        return {}
    return {d.name: d for d in sorted(tools_dir.iterdir()) if d.is_dir()}


def _discover_agent_callbacks(agent_dir: Path) -> dict[str, Path]:
    """Map `{cb_type/slot: python_code.py}` across all CES export layouts."""
    out: dict[str, Path] = {}
    for cb_type in CALLBACK_DIRS:
        cb_dir = agent_dir / cb_type
        if cb_dir.is_dir():
            for entry in sorted(cb_dir.iterdir()):
                if entry.is_dir():
                    code = entry / "python_code.py"
                    if code.is_file():
                        out[f"{cb_type}/{entry.name}"] = code
                elif entry.is_file() and entry.suffix == ".py":
                    out[f"{cb_type}/{entry.stem}"] = entry
        for entry in sorted(agent_dir.iterdir()):
            if entry.is_dir() and entry.name.startswith(f"{cb_type}_"):
                code = entry / "python_code.py"
                if code.is_file():
                    out[f"{cb_type}/{entry.name}"] = code
    return out


def _parse_agent_map(raw_maps: list[str] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in raw_maps or []:
        if "=" not in item:
            raise ValueError(
                f"Invalid --agent-map entry {item!r}; use VOICE_NAME=CHAT_NAME."
            )
        v_name, c_name = item.split("=", 1)
        mapping[v_name.strip()] = c_name.strip()
    return mapping


def _canonical_agent_key(name: str) -> str:
    """Strip common `_voice` / `_chat` / `_text` suffixes for auto-pairing."""
    cleaned = re.sub(
        r"(?:^|_)(?:voice|chat|text|ivr|iva)(?:_|$)",
        "_",
        name.lower(),
    )
    return re.sub(r"_+", "_", cleaned).strip("_") or name.lower()


def pair_agents(
    voice_root: Path,
    chat_root: Path,
    explicit_map: dict[str, str] | None = None,
) -> list[AgentPair]:
    """Pair agents across Voice and Chat directories."""
    v_agents = _discover_agents(voice_root)
    c_agents = _discover_agents(chat_root)
    matched_c: set[str] = set()
    pairs: list[AgentPair] = []
    explicit = explicit_map or {}

    for v_name, v_dir in v_agents.items():
        if v_name in explicit:
            target = explicit[v_name]
            if target in c_agents and target not in matched_c:
                matched_c.add(target)
                pairs.append(
                    AgentPair(v_name, v_name, target, v_dir, c_agents[target])
                )
                continue
        if v_name in c_agents and v_name not in matched_c:
            matched_c.add(v_name)
            pairs.append(
                AgentPair(v_name, v_name, v_name, v_dir, c_agents[v_name])
            )
            continue
        v_key = _canonical_agent_key(v_name)
        canon_hits = [
            c
            for c in c_agents
            if c not in matched_c and _canonical_agent_key(c) == v_key
        ]
        if len(canon_hits) == 1:
            c_name = canon_hits[0]
            matched_c.add(c_name)
            merged_name = re.sub(
                r"_(?:voice|ivr)$", "", v_name, flags=re.IGNORECASE
            )
            pairs.append(
                AgentPair(
                    merged_name or v_name,
                    v_name,
                    c_name,
                    v_dir,
                    c_agents[c_name],
                )
            )
        else:
            pairs.append(AgentPair(v_name, v_name, None, v_dir, None))

    for c_name, c_dir in c_agents.items():
        if c_name not in matched_c:
            pairs.append(AgentPair(c_name, None, c_name, None, c_dir))

    return pairs


# ── Instruction XML Parsing & Lossless Diff/Merge ────────────────────────


def split_top_level_sections(text: str) -> list[Section]:
    """Split an instruction into top-level XML tag blocks and text blocks."""
    lines = text.splitlines()
    sections: list[Section] = []
    plain_buf: list[str] = []
    idx = 0
    seen_counts: dict[str, int] = {}

    def _flush_plain() -> None:
        if plain_buf:
            n = seen_counts.get("__text__", 0)
            seen_counts["__text__"] = n + 1
            sections.append(
                Section("text", "", "", "", list(plain_buf), f"text:{n}")
            )
            plain_buf.clear()

    while idx < len(lines):
        line = lines[idx]
        m = _XML_SECTION_RE.match(line)
        if m and m.group("tag").lower() in STRUCTURAL_XML_TAGS:
            tag = m.group("tag")
            close_pat = re.compile(
                rf"</\s*{re.escape(tag)}\s*>\s*$", re.IGNORECASE
            )
            open_pat = re.compile(
                rf"^\s*<{re.escape(tag)}(?:\s+[^>]*)?>", re.IGNORECASE
            )
            # Single-line XML tag like `<role>Assistant.</role>`
            if close_pat.search(line):
                _flush_plain()
                gt_pos = line.index(">")
                close_m = close_pat.search(line)
                assert close_m is not None
                inner = line[gt_pos + 1 : close_m.start()]
                count = seen_counts.get(tag.lower(), 0)
                seen_counts[tag.lower()] = count + 1
                open_tag = line[: gt_pos + 1]
                close_tag = line[close_m.start() :]
                body = [inner] if inner else []
                sections.append(
                    Section(
                        "xml",
                        tag,
                        open_tag,
                        close_tag,
                        body,
                        f"xml:{tag.lower()}:{count}",
                    )
                )
                idx += 1
                continue
            # Multi-line XML tag
            depth = 1
            body_lines: list[str] = []
            rest_after_open = m.group("rest")
            if rest_after_open.strip():
                body_lines.append(rest_after_open)
            j = idx + 1
            close_line = ""
            found_close = False
            while j < len(lines):
                cand = lines[j]
                if open_pat.match(cand) and not close_pat.search(cand):
                    depth += 1
                    body_lines.append(cand)
                elif close_pat.search(cand):
                    depth -= 1
                    if depth == 0:
                        close_m = close_pat.search(cand)
                        assert close_m is not None
                        prefix = cand[: close_m.start()]
                        if prefix.strip():
                            body_lines.append(prefix)
                        close_line = (
                            f"{m.group('indent')}</{tag}>"
                            if not prefix.strip()
                            else cand[close_m.start() :]
                        )
                        found_close = True
                        break
                    body_lines.append(cand)
                else:
                    body_lines.append(cand)
                j += 1
            if found_close:
                _flush_plain()
                count = seen_counts.get(tag.lower(), 0)
                seen_counts[tag.lower()] = count + 1
                open_line = f"{m.group('indent')}<{tag}{m.group('attrs')}>"
                sections.append(
                    Section(
                        "xml",
                        tag,
                        open_line,
                        close_line,
                        body_lines,
                        f"xml:{tag.lower()}:{count}",
                    )
                )
                idx = j + 1
                continue

        plain_buf.append(line)
        idx += 1

    _flush_plain()
    return sections


def _wrap_block(lines: list[str], modality: str, tag_style: str) -> list[str]:
    """Wrap a sequence of lines inside a VOICE or TEXT modality marker."""
    if not lines:
        return []
    if tag_style == "jinja":
        return [
            f"{{{{% if _session.modality == '{modality}' %}}}}",
            *lines,
            "{{% endif %}}",
        ]
    label = "Voice" if modality == "VOICE" else "Text"
    return [
        f"{{@startModality_{label}}}",
        *lines,
        f"{{@endModality_{label}}}",
    ]


def _wrap_replace(
    voice_lines: list[str], chat_lines: list[str], tag_style: str
) -> list[str]:
    """Wrap diverging voice vs. chat lines."""
    if tag_style == "jinja":
        return [
            "{{% if _session.modality == 'VOICE' %}}",
            *voice_lines,
            "{{% elif _session.modality == 'TEXT' %}}",
            *chat_lines,
            "{{% endif %}}",
        ]
    return [
        "{@startModality_Voice}",
        *voice_lines,
        "{@endModality_Voice}",
        "{@startModality_Text}",
        *chat_lines,
        "{@endModality_Text}",
    ]


def merge_line_lists(
    voice_lines: list[str], chat_lines: list[str], tag_style: str = "ces-tags"
) -> list[str]:
    """Losslessly merge two line lists with minimal modality wrapping."""
    sm = difflib.SequenceMatcher(None, voice_lines, chat_lines, autojunk=False)
    out: list[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.extend(voice_lines[i1:i2])
        elif op == "delete":
            out.extend(_wrap_block(voice_lines[i1:i2], "VOICE", tag_style))
        elif op == "insert":
            out.extend(_wrap_block(chat_lines[j1:j2], "TEXT", tag_style))
        elif op == "replace":
            v_chunk = voice_lines[i1:i2]
            c_chunk = chat_lines[j1:j2]
            # Emit untouched blank lines outside tag wrappers when identical.
            if all(not x.strip() for x in v_chunk) and all(
                not x.strip() for x in c_chunk
            ):
                out.extend(v_chunk)
            else:
                out.extend(_wrap_replace(v_chunk, c_chunk, tag_style))
    return out


def merge_instructions(
    voice_text: str, chat_text: str, tag_style: str = "ces-tags"
) -> str:
    """Merge sibling Voice and Chat instructions keeping top-level XML static.

    Both source strings may be plain text/XML with zero existing Jinja.
    """
    if voice_text == chat_text:
        return voice_text

    v_secs = split_top_level_sections(voice_text)
    c_secs = split_top_level_sections(chat_text)
    v_keys = [s.key for s in v_secs]
    c_keys = [s.key for s in c_secs]

    # Fast lossless alignment when structural XML blocks line up by key.
    has_xml = any(s.kind == "xml" for s in v_secs) and any(
        s.kind == "xml" for s in c_secs
    )
    if not has_xml:
        merged = merge_line_lists(
            voice_text.splitlines(), chat_text.splitlines(), tag_style
        )
        return "\n".join(merged) + ("\n" if merged else "")

    sm = difflib.SequenceMatcher(None, v_keys, c_keys, autojunk=False)
    out: list[str] = []

    def _emit_unilateral(sec: Section, modality: str) -> None:
        if sec.kind == "xml" and sec.tag.lower() in TOP_LEVEL_PROTECTED_TAGS:
            # Keep structural XML static (I024); wrap body inside!
            out.append(sec.open_line)
            out.extend(_wrap_block(sec.body_lines, modality, tag_style))
            out.append(sec.close_line)
        elif sec.kind == "xml":
            block = [sec.open_line, *sec.body_lines, sec.close_line]
            out.extend(_wrap_block(block, modality, tag_style))
        elif any(line.strip() for line in sec.body_lines):
            out.extend(_wrap_block(sec.body_lines, modality, tag_style))
        else:
            out.extend(sec.body_lines)

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for vs, cs in zip(v_secs[i1:i2], c_secs[j1:j2], strict=True):
                if vs.kind == "xml":
                    out.append(vs.open_line)
                    out.extend(
                        merge_line_lists(
                            vs.body_lines, cs.body_lines, tag_style
                        )
                    )
                    out.append(vs.close_line)
                else:
                    out.extend(
                        merge_line_lists(
                            vs.body_lines, cs.body_lines, tag_style
                        )
                    )
        elif op == "delete":
            for vs in v_secs[i1:i2]:
                _emit_unilateral(vs, "VOICE")
        elif op == "insert":
            for cs in c_secs[j1:j2]:
                _emit_unilateral(cs, "TEXT")
        elif op == "replace":
            # Re-pair matching XML tags inside a reordered span if present.
            c_slice = c_secs[j1:j2]
            c_used: set[int] = set()
            for vs in v_secs[i1:i2]:
                match_idx = next(
                    (
                        idx
                        for idx, cs in enumerate(c_slice)
                        if idx not in c_used
                        and vs.kind == "xml"
                        and cs.kind == "xml"
                        and vs.tag.lower() == cs.tag.lower()
                    ),
                    None,
                )
                if match_idx is not None:
                    c_used.add(match_idx)
                    cs = c_slice[match_idx]
                    out.append(vs.open_line)
                    out.extend(
                        merge_line_lists(
                            vs.body_lines, cs.body_lines, tag_style
                        )
                    )
                    out.append(vs.close_line)
                else:
                    _emit_unilateral(vs, "VOICE")
            for idx, cs in enumerate(c_slice):
                if idx not in c_used:
                    _emit_unilateral(cs, "TEXT")

    return "\n".join(out) + ("\n" if out else "")


# ── CES-Parity Modality Renderer ─────────────────────────────────────────


def render_for_modality(
    template: str, modality: str, channel: str | None = None
) -> str:
    """Evaluate `{@startModality_*}`, `{@startChannel_*}`, and `_session` Jinja
    control lines under the given modality (``VOICE`` or ``TEXT``).

    Other custom Jinja control blocks (e.g. ``{% if brand == 'A' %}``) are left
    untouched so offline verification checks exact branch isolation cleanly.
    """
    mod = modality.upper()
    chan = channel.upper() if channel else None

    # Line-based stack evaluating whole-line tags inserted by merge or authors.
    # Each entry: [is_session_controlled, currently_active, branch_taken]
    stack: list[list[bool]] = []
    out_lines: list[str] = []

    def _active() -> bool:
        return all(frame[1] for frame in stack if frame[0])

    # Handle inline tag spans inside a single line cleanly.
    def _eval_inline_ces_tags(line: str) -> str:
        res: list[str] = []
        pos = 0
        inl_stack: list[bool] = []
        for m in _ANY_CES_TAG_RE.finditer(line):
            if all(inl_stack):
                res.append(line[pos : m.start()])
            direction, kind, raw_key = m.group(1), m.group(2), m.group(3)
            if direction == "start":
                if kind == "Modality":
                    canon = MODALITY_MAP.get(raw_key)
                    inl_stack.append(canon == mod if canon else True)
                else:
                    canon = CHANNEL_MAP.get(raw_key)
                    inl_stack.append(
                        True
                        if (chan is None or canon is None)
                        else canon == chan
                    )
            elif inl_stack:
                inl_stack.pop()
            pos = m.end()
        if pos == 0:
            return line
        if all(inl_stack):
            res.append(line[pos:])
        return "".join(res)

    for raw_line in template.splitlines():
        stripped = raw_line.strip()

        # Whole-line `{@startModality_*}` / `{@endModality_*}`
        tag_m = _ANY_CES_TAG_RE.fullmatch(stripped)
        if tag_m:
            direction, kind, raw_key = (
                tag_m.group(1),
                tag_m.group(2),
                tag_m.group(3),
            )
            if direction == "start":
                if kind == "Modality":
                    canon = MODALITY_MAP.get(raw_key)
                    cond = canon == mod if canon else False
                    stack.append([True, cond, cond])
                else:
                    canon = CHANNEL_MAP.get(raw_key)
                    if chan is None:
                        stack.append([False, True, True])
                        if _active():
                            out_lines.append(raw_line)
                    else:
                        cond = canon == chan if canon else False
                        stack.append([True, cond, cond])
            elif stack:
                frame = stack.pop()
                if not frame[0] and _active():
                    out_lines.append(raw_line)
            continue

        # Whole-line `{% ... %}` / `{{% ... %}}` checking `_session.modality`
        blk_m = _BLOCK_TAG_RE.fullmatch(stripped)
        if blk_m:
            body = blk_m.group(1).strip()
            parts = body.split(None, 1)
            kw = parts[0] if parts else ""
            rest = parts[1].strip() if len(parts) > 1 else ""
            if kw == "if":
                mm = _MODALITY_IF_RE.match(rest)
                cm = _CHANNEL_IF_RE.match(rest)
                if mm:
                    op, target = mm.group(1), mm.group(2).upper()
                    cond = (mod == target) if op == "==" else (mod != target)
                    stack.append([True, cond, cond])
                    continue
                if cm and chan is not None:
                    op, target = cm.group(1), cm.group(2).upper()
                    cond = (chan == target) if op == "==" else (chan != target)
                    stack.append([True, cond, cond])
                    continue
                stack.append([False, True, True])
                if _active():
                    out_lines.append(raw_line)
                continue
            if kw in ("elif",) and stack and stack[-1][0]:
                mm = _MODALITY_IF_RE.match(rest)
                if mm:
                    op, target = mm.group(1), mm.group(2).upper()
                    cond = (mod == target) if op == "==" else (mod != target)
                    frame = stack[-1]
                    if frame[2]:
                        frame[1] = False
                    else:
                        frame[1] = cond
                        frame[2] = cond
                    continue
            if kw == "else" and stack and stack[-1][0]:
                frame = stack[-1]
                if frame[2]:
                    frame[1] = False
                else:
                    frame[1] = True
                    frame[2] = True
                continue
            if kw == "endif" and stack:
                frame = stack.pop()
                if not frame[0] and _active():
                    out_lines.append(raw_line)
                continue

        if _active():
            out_lines.append(_eval_inline_ces_tags(raw_line))

    return "\n".join(out_lines) + ("\n" if out_lines else "")


# ── Instruction & Callback Static Validators ─────────────────────────────


def validate_instruction_text(
    text: str, location: str, findings: list[Finding]
) -> None:
    """Validate an instruction or global_instruction against CES rules."""
    masked = _mask_exempt(text)

    # 1. Legacy/unsupported Jinja comments (`{# ... #}`, `{{# ... #}}`)
    for m in _COMMENT_RE.finditer(masked):
        findings.append(
            Finding(
                code="M_JINJA_COMMENT",
                severity="ERROR",
                category="NEEDS_APPROVAL",
                location=location,
                line=_line_of(masked, m.start()),
                message=(
                    "Jinja comments ({# ... #} / {{# ... #}}) are NOT stripped "
                    "by CES and leak into prompts or TTS."
                ),
                fix="Remove comments from deployed instruction files.",
            )
        )

    # 2. Control block checks (`{% ... %}` and `{{% ... %}}`)
    block_stack: list[tuple[str, int]] = []
    for m in _BLOCK_TAG_RE.finditer(masked):
        raw_body = m.group(1).strip()
        line = _line_of(masked, m.start())
        if not raw_body:
            continue
        parts = raw_body.split(None, 2)
        kw = parts[0]
        if kw == "else" and len(parts) >= 2 and parts[1] == "if":
            kw = "elif"
            rest = parts[2] if len(parts) > 2 else ""
        else:
            rest = raw_body[len(kw) :].strip()

        if kw not in SUPPORTED_BLOCK_KEYWORDS:
            findings.append(
                Finding(
                    code="M_UNSUPPORTED_TAG",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message=f"Unsupported Jinja block keyword '{{% {kw} %}}'.",
                    fix=(
                        "Supported: if, elif, else, endif, for, endfor, macro, "
                        "endmacro. Move {% set %} into before_agent_callback."
                    ),
                )
            )
            continue

        rest_nq = _strip_quotes(rest)
        if kw in ("if", "elif", "for"):
            if _NESTED_BRACE_RE.search(rest_nq):
                findings.append(
                    Finding(
                        code="M_NESTED_BRACES",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message="Nested curly braces inside control tag.",
                        fix="Use bare variable names inside {% ... %}.",
                    )
                )
            if _NOT_IN_RE.search(rest_nq):
                findings.append(
                    Finding(
                        code="M_NOT_IN",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message="CES Jinja rejects `not in` operator.",
                        fix="Rewrite as `not (x in [...])`.",
                    )
                )
            if _DICT_SUBSCRIPT_RE.search(rest):
                findings.append(
                    Finding(
                        code="M_DICT_SUBSCRIPT",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message="CES Jinja rejects dict['key'] subscripts.",
                        fix="Use dot notation (e.g. `order.status`).",
                    )
                )
            if _NEG_LITERAL_RE.search(rest_nq):
                findings.append(
                    Finding(
                        code="M_NEG_LITERAL",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message=(
                            "Negative numeric literal in expression fails CES "
                            "expression lexer (`-` is unsupported)."
                        ),
                        fix=(
                            "Compare against 0 or store negative threshold in "
                            "a declared session variable."
                        ),
                    )
                )
            for hm in _HYPHEN_VAR_RE.finditer(rest_nq):
                findings.append(
                    Finding(
                        code="M_HYPHEN_VAR",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message=(
                            f"Hyphenated variable '{hm.group(1)}' inside "
                            f"'{{% {kw} %}}' fails expression parsing."
                        ),
                        fix=f"Rename to '{hm.group(1).replace('-', '_')}'.",
                    )
                )
            _check_filters_and_loops(rest_nq, location, line, findings)

        if kw in ("if", "for", "macro"):
            block_stack.append((kw, line))
        elif kw in ("elif", "else"):
            if not block_stack or block_stack[-1][0] != "if":
                findings.append(
                    Finding(
                        code="M_UNBALANCED_BLOCK",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message=f"Unexpected '{{% {kw} %}}' outside an 'if'.",
                        fix="Ensure matching {% if %} precedes it.",
                    )
                )
        elif kw in ("endif", "endfor", "endmacro"):
            expected = kw[len("end") :]
            if not block_stack or block_stack[-1][0] != expected:
                findings.append(
                    Finding(
                        code="M_UNBALANCED_BLOCK",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message=(
                            f"Unexpected '{{% {kw} %}}' without open block."
                        ),
                        fix="Balance Jinja block open/close tags.",
                    )
                )
            else:
                block_stack.pop()

    for open_kw, open_line in block_stack:
        findings.append(
            Finding(
                code="M_UNBALANCED_BLOCK",
                severity="ERROR",
                category="NEEDS_APPROVAL",
                location=location,
                line=open_line,
                message=f"Unclosed '{{% {open_kw} %}}' block.",
                fix=f"Add matching '{{% end{open_kw} %}}'.",
            )
        )

    # 3. Print tag checks (`{{ ... }}`)
    for m in _PRINT_TAG_RE.finditer(masked):
        raw_expr = m.group(1).strip()
        if not raw_expr:
            continue
        line = _line_of(masked, m.start())
        expr_nq = _strip_quotes(raw_expr)
        if _DICT_SUBSCRIPT_RE.search(raw_expr):
            findings.append(
                Finding(
                    code="M_DICT_SUBSCRIPT",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message="CES Jinja rejects dict['key'] subscript in print.",
                    fix="Use dot notation (e.g. `{{ customer.first_name }}`).",
                )
            )
        if _UPPER_CALL_RE.search(expr_nq):
            findings.append(
                Finding(
                    code="M_UPPER_CALLABLE",
                    severity="WARNING",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message=(
                        "`upper(...)` is not callable in CES; use the pipe "
                        "filter `| upper`."
                    ),
                    fix="Rewrite as `{{ value | upper }}`.",
                )
            )
        if _KWARG_EQ_RE.search(expr_nq):
            findings.append(
                Finding(
                    code="M_FILTER_KWARG",
                    severity="WARNING",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message=(
                        "Keyword arguments (e.g., `boolean_eval=true`) fail in "
                        "CES expressions and degrade to literal text."
                    ),
                    fix=(
                        "Use positional args only: "
                        '`| default("Fallback", true)`.'
                    ),
                )
            )
        _check_filters_and_loops(expr_nq, location, line, findings)

    # 4. Modality/channel snippet tag allowlist & stack parity
    tag_stack: list[tuple[str, str, str, str, int, int]] = []
    for m in _ANY_CES_TAG_RE.finditer(masked):
        direction, kind, raw_key = m.group(1), m.group(2), m.group(3)
        raw_tag = m.group(0)
        line = _line_of(masked, m.start())
        canon = (
            MODALITY_MAP.get(raw_key)
            if kind == "Modality"
            else CHANNEL_MAP.get(raw_key)
        )
        if canon is None:
            hint = (
                "Use `{@startModality_Voice}` instead of `Telephony`."
                if raw_key.lower() == "telephony"
                else f"Check valid CES {kind.lower()} tag names."
            )
            findings.append(
                Finding(
                    code="M_INVALID_CES_TAG",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message=f"Invalid {kind.lower()} tag '{raw_tag}'.",
                    fix=hint,
                )
            )
            continue
        if direction == "start":
            tag_stack.append((kind, canon, raw_key, raw_tag, m.end(), line))
        elif not tag_stack:
            findings.append(
                Finding(
                    code="M_STRAY_CLOSER",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message=f"Stray closing tag '{raw_tag}'.",
                    fix="Remove extra closing tag or add its start tag.",
                )
            )
        else:
            o_kind, o_canon, o_key, o_tag, b_start, o_line = tag_stack.pop()
            if (o_kind, o_canon) != (kind, canon):
                findings.append(
                    Finding(
                        code="M_MISMATCHED_CLOSER",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message=(
                            f"Mismatched closer '{raw_tag}' for '{o_tag}' "
                            f"opened on line {o_line}."
                        ),
                        fix=f"Replace with `{{@end{o_kind}_{o_key}}}`.",
                    )
                )
            else:
                body = masked[b_start : m.start()]
                xml_m = _TOP_XML_OPEN_RE.search(body)
                if xml_m:
                    findings.append(
                        Finding(
                            code="M_XML_WRAPPED_IN_TAG",
                            severity="WARNING",
                            category="NEEDS_APPROVAL",
                            location=location,
                            line=_line_of(masked, b_start + xml_m.start()),
                            message=(
                                f"Top-level XML '<{xml_m.group(1)}>' wrapped "
                                f"inside '{o_tag}'."
                            ),
                            fix="Keep top-level XML static; nest tags inside.",
                        )
                    )

    for o_kind, _c, o_key, o_tag, _s, o_line in tag_stack:
        findings.append(
            Finding(
                code="M_UNCLOSED_CES_TAG",
                severity="ERROR",
                category="NEEDS_APPROVAL",
                location=location,
                line=o_line,
                message=f"Unclosed tag '{o_tag}'.",
                fix=f"Add matching `{{@end{o_kind}_{o_key}}}`.",
            )
        )

    # 5. Legacy `${var}` syntax & banned legacy TTS tags
    for m in _LEGACY_DOLLAR_VAR_RE.finditer(masked):
        findings.append(
            Finding(
                code="M_LEGACY_DOLLAR_VAR",
                severity="WARNING",
                category="NEEDS_APPROVAL",
                location=location,
                line=_line_of(masked, m.start()),
                message=f"Legacy `${{{m.group(1)}}}` placeholder syntax found.",
                fix=f"Use `{{{{{m.group(1)}}}}}` (or `{{{m.group(1)}}}`).",
            )
        )
    for m in _LEGACY_TTS_TAG_RE.finditer(masked):
        findings.append(
            Finding(
                code="M_LEGACY_TTS_TAG",
                severity="WARNING",
                category="NEEDS_APPROVAL",
                location=location,
                line=_line_of(masked, m.start()),
                message=(
                    f"Legacy/unsupported TTS markup '{m.group(0)}' (can be "
                    "read aloud on gemini-composite-v2)."
                ),
                fix="Remap via cxas-composite-voice-agent-optimizer.",
            )
        )

    # 6. Trailing raw `}}` line check (excludes Jinja prints/blocks/tags)
    for lineno, line_txt in enumerate(masked.splitlines(), start=1):
        s = line_txt.rstrip()
        if (
            s.endswith("}}")
            and not s.endswith("%}}")
            and not _PRINT_TAG_RE.search(s)
        ):
            findings.append(
                Finding(
                    code="M_TRAILING_BRACES",
                    severity="WARNING",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=lineno,
                    message=(
                        "Line ends with raw `}}` closing braces (known "
                        "instruction-leakage risk)."
                    ),
                    fix="End with punctuation or remove trailing raw `}}`.",
                )
            )


def _check_filters_and_loops(
    expr_no_quotes: str,
    location: str,
    line: int,
    findings: list[Finding],
) -> None:
    if "|" in expr_no_quotes:
        for seg in expr_no_quotes.split("|")[1:]:
            fm = _FILTER_NAME_RE.match(seg)
            if fm and fm.group(1) not in SUPPORTED_FILTERS:
                findings.append(
                    Finding(
                        code="M_UNSUPPORTED_FILTER",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location=location,
                        line=line,
                        message=(
                            f"Unsupported Jinja filter '| {fm.group(1)}' "
                            "(degrades to literal text at runtime)."
                        ),
                        fix=(
                            "Supported filters: length, upper, default. Move "
                            "other formatting into before_agent_callback."
                        ),
                    )
                )
    for lm in _LOOP_ATTR_RE.finditer(expr_no_quotes):
        if lm.group(1) not in SUPPORTED_LOOP_ATTRS:
            findings.append(
                Finding(
                    code="M_UNSUPPORTED_LOOP_ATTR",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=line,
                    message=(
                        f"Unsupported `loop.{lm.group(1)}` (only `loop.last` "
                        "is supported)."
                    ),
                    fix="Use `loop.last` or pre-compute index in callback.",
                )
            )


def _check_policy_and_brace_drift(
    voice_text: str,
    chat_text: str,
    location: str,
    findings: list[Finding],
) -> None:
    """Compare numeric policy tokens and `{var}` vs `{{var}}` brace styles."""
    v_tokens = {
        m.group(0).lower().strip()
        for m in _POLICY_VALUE_RE.finditer(voice_text)
    }
    c_tokens = {
        m.group(0).lower().strip() for m in _POLICY_VALUE_RE.finditer(chat_text)
    }
    v_only = sorted(v_tokens - c_tokens)
    c_only = sorted(c_tokens - v_tokens)
    if v_only or c_only:
        findings.append(
            Finding(
                code="M_POLICY_DRIFT",
                severity="WARNING",
                category="NEEDS_APPROVAL",
                location=location,
                message=(
                    "Numeric/policy values differ between Voice and Chat — "
                    f"voice-only={v_only or 'none'}, "
                    f"chat-only={c_only or 'none'}."
                ),
                fix=(
                    "Confirm whether the divergence is intentional before "
                    "unifying; merged instruction keeps both branches intact."
                ),
            )
        )

    v_single = {
        m.group(1) for m in _SINGLE_VAR_RE.finditer(_mask_exempt(voice_text))
    }
    v_double = {
        m.group(1) for m in _DOUBLE_VAR_RE.finditer(_mask_exempt(voice_text))
    }
    c_single = {
        m.group(1) for m in _SINGLE_VAR_RE.finditer(_mask_exempt(chat_text))
    }
    c_double = {
        m.group(1) for m in _DOUBLE_VAR_RE.finditer(_mask_exempt(chat_text))
    }
    mixed = sorted((v_single & c_double) | (c_single & v_double))
    for var_name in mixed:
        findings.append(
            Finding(
                code="M_BRACE_DRIFT",
                severity="WARNING",
                category="NEEDS_APPROVAL",
                location=location,
                message=(
                    f"Variable '{var_name}' uses single-brace `{{{var_name}}}` "
                    f"in one app and double-brace `{{{{{var_name}}}}}` in the "
                    "other."
                ),
                fix=(
                    "Standardize on `{{var}}` for inline substitution (0 bytes "
                    "of <state_update>) or `{var}` for dynamic tracking."
                ),
            )
        )


def _check_before_agent_guard(
    py_path: Path, location: str, findings: list[Finding]
) -> None:
    """Flag unguarded empty/zero resets in `before_agent_callback`."""
    try:
        src = py_path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(src)
    except SyntaxError:
        return

    for node in tree.body:
        if (
            not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            or node.name != "before_agent_callback"
        ):
            continue
        aliases = {"state", "variables"}
        for stmt in node.body:
            if isinstance(stmt, ast.If) and any(
                isinstance(s, ast.Return) for s in stmt.body
            ):
                dump = ast.dump(stmt.test).lower()
                if any(tok in dump for tok in _INIT_GUARD_TOKENS):
                    break
            if isinstance(stmt, ast.Assign):
                if (
                    len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Attribute)
                    and stmt.value.attr in ("state", "variables")
                ):
                    aliases.add(stmt.targets[0].id)
                    continue
                is_empty = (
                    (
                        isinstance(stmt.value, ast.Constant)
                        and stmt.value.value in ("", 0, 0.0, False)
                        and stmt.value.value is not True
                    )
                    or (
                        isinstance(stmt.value, ast.List) and not stmt.value.elts
                    )
                    or (
                        isinstance(stmt.value, ast.Dict) and not stmt.value.keys
                    )
                )
                if not is_empty:
                    continue
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                        and (
                            (
                                isinstance(target.value, ast.Name)
                                and target.value.id in aliases
                            )
                            or (
                                isinstance(target.value, ast.Attribute)
                                and target.value.attr in ("state", "variables")
                            )
                        )
                    ):
                        key = target.slice.value
                        findings.append(
                            Finding(
                                code="M_MISSING_TURN_GUARD",
                                severity="WARNING",
                                category="NEEDS_APPROVAL",
                                location=location,
                                line=stmt.lineno,
                                message=(
                                    f"before_agent_callback resets '{key}' "
                                    "without a one-time `_initialized` guard "
                                    "(wipes state on Turn 2+)."
                                ),
                                fix=(
                                    'Add `if state.get("_initialized") == '
                                    '"true": return None` at the top.'
                                ),
                            )
                        )


def _check_neutral_verbs_in_shared(
    merged_text: str, location: str, findings: list[Finding]
) -> None:
    """Advise softening voice/chat-biased verbs in shared unwrapped lines."""
    depth = 0
    for lineno, raw_line in enumerate(merged_text.splitlines(), start=1):
        s = raw_line.strip()
        m = _ANY_CES_TAG_RE.fullmatch(s)
        if m:
            depth += 1 if m.group(1) == "start" else -1
            depth = max(0, depth)
            continue
        blk = _BLOCK_TAG_RE.fullmatch(s)
        if blk:
            b = blk.group(1).strip()
            if b.startswith("if ") and "_session." in b:
                depth += 1
                continue
            if b == "endif" and depth > 0:
                depth -= 1
                continue
        if depth > 0:
            continue
        if _BIASED_VOICE_VERB_RE.search(
            raw_line
        ) or _BIASED_TEXT_VERB_RE.search(raw_line):
            findings.append(
                Finding(
                    code="M_BIASED_SHARED_VERB",
                    severity="INFO",
                    category="NEEDS_APPROVAL",
                    location=location,
                    line=lineno,
                    message=(
                        f"Shared line uses channel-biased phrasing: {s[:72]!r}."
                    ),
                    fix=(
                        "Consider neutral verbs (`respond`, `inform`, "
                        "`provide`, `share`) or move into modality tags."
                    ),
                )
            )


def _check_variable_declarations(
    voice_cfg: dict[str, Any],
    chat_cfg: dict[str, Any],
    report: AuditReport,
) -> list[dict[str, Any]]:
    """Audit & union `variableDeclarations`, reporting schema conflicts."""
    v_vars = {
        v["name"]: v
        for v in voice_cfg.get("variableDeclarations", [])
        if isinstance(v, dict) and v.get("name")
    }
    c_vars = {
        v["name"]: v
        for v in chat_cfg.get("variableDeclarations", [])
        if isinstance(v, dict) and v.get("name")
    }
    merged_list: list[dict[str, Any]] = []
    all_names = sorted(set(v_vars) | set(c_vars))

    for name in all_names:
        if name in RESERVED_VARIABLES:
            report.findings.append(
                Finding(
                    code="M_RESERVED_VAR_DECLARED",
                    severity="ERROR",
                    category="SAFE_AUTO",
                    location="app.json",
                    message=(
                        f"Reserved variable '{name}' must NOT be declared in "
                        "variableDeclarations (CES rejects with 400)."
                    ),
                    fix=f"Omitted '{name}' from merged variableDeclarations.",
                )
            )
            continue
        if ":" in name and name.split(":", 1)[0] in ("app", "user", "temp"):
            report.findings.append(
                Finding(
                    code="M_COLON_VAR_NAME",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location="app.json",
                    message=(
                        f"Variable '{name}' uses colon prefix which fails "
                        "UpdateApp regex validation."
                    ),
                    fix="Rename to a snake_case variable without colon prefix.",
                )
            )
        if "-" in name:
            report.findings.append(
                Finding(
                    code="M_HYPHEN_VAR_DECL",
                    severity="WARNING",
                    category="NEEDS_APPROVAL",
                    location="app.json",
                    message=(
                        f"Declared variable '{name}' contains `-`, which fails "
                        "in Jinja filters and `{% if %}` expressions."
                    ),
                    fix=f"Prefer snake_case '{name.replace('-', '_')}'.",
                )
            )

        in_v, in_c = name in v_vars, name in c_vars
        if in_v and in_c:
            vt = str((v_vars[name].get("schema") or {}).get("type", ""))
            ct = str((c_vars[name].get("schema") or {}).get("type", ""))
            if vt and ct and vt != ct:
                report.variable_conflicts.append(
                    {"name": name, "voice_type": vt, "chat_type": ct}
                )
                report.findings.append(
                    Finding(
                        code="M_VAR_TYPE_CONFLICT",
                        severity="ERROR",
                        category="NEEDS_APPROVAL",
                        location="app.json",
                        message=(
                            f"Variable '{name}' type mismatch: Voice={vt!r} "
                            f"vs Chat={ct!r}."
                        ),
                        fix=(
                            "Reconcile schema type manually before pushing "
                            "(use STRING for IDs/ZIPs to avoid float coercion)."
                        ),
                    )
                )
            else:
                report.variables_shared.append(name)
            merged_list.append(v_vars[name])
        elif in_v:
            report.variables_voice_only.append(name)
            merged_list.append(v_vars[name])
        else:
            report.variables_text_only.append(name)
            merged_list.append(c_vars[name])

    report.total_variables = len(merged_list)
    if len(merged_list) > VARIABLE_COUNT_LIMIT:
        report.findings.append(
            Finding(
                code="M_VAR_QUOTA_CEILING",
                severity="WARNING",
                category="NEEDS_APPROVAL",
                location="app.json",
                message=(
                    f"Merged app declares {len(merged_list)} variables "
                    f"(recommended safety ceiling is {VARIABLE_COUNT_LIMIT})."
                ),
                fix=(
                    "Consolidate related keys into a structured OBJECT "
                    "variable."
                ),
            )
        )
    return merged_list


# ── Audit Engine ─────────────────────────────────────────────────────────


def run_audit(
    voice_dir: Path,
    chat_dir: Path,
    explicit_map: dict[str, str] | None = None,
) -> AuditReport:
    """Perform a read-only audit of two sibling Voice and Chat CXAS apps."""
    v_root = resolve_app_root(voice_dir)
    c_root = resolve_app_root(chat_dir)
    report = AuditReport(voice_root=str(v_root), chat_root=str(c_root))

    v_app = _load_json(v_root / "app.json")
    c_app = _load_json(c_root / "app.json")
    _check_variable_declarations(v_app, c_app, report)

    # Tools parity
    v_tools = _discover_tools(v_root)
    c_tools = _discover_tools(c_root)
    for t_name in sorted(set(v_tools) | set(c_tools)):
        in_v, in_c = t_name in v_tools, t_name in c_tools
        if in_v and in_c:
            report.tools_shared.append(t_name)
            if _sha256_tree(v_tools[t_name]) != _sha256_tree(c_tools[t_name]):
                report.tool_conflicts.append(t_name)
                report.findings.append(
                    Finding(
                        code="M_TOOL_CONFLICT",
                        severity="WARNING",
                        category="NEEDS_APPROVAL",
                        location=f"tools/{t_name}",
                        message=(
                            f"Shared tool '{t_name}' differs between Voice "
                            "and Chat implementations."
                        ),
                        fix=(
                            "Diff both copies manually and confirm a single "
                            "reconciled implementation."
                        ),
                    )
                )
        elif in_v:
            report.tools_voice_only.append(t_name)
        else:
            report.tools_text_only.append(t_name)

    # Global instructions
    v_glob_p = v_root / "global_instruction.txt"
    c_glob_p = c_root / "global_instruction.txt"
    v_glob = v_glob_p.read_text(encoding="utf-8") if v_glob_p.is_file() else ""
    c_glob = c_glob_p.read_text(encoding="utf-8") if c_glob_p.is_file() else ""
    if v_glob:
        validate_instruction_text(
            v_glob, "voice:global_instruction.txt", report.findings
        )
    if c_glob:
        validate_instruction_text(
            c_glob, "chat:global_instruction.txt", report.findings
        )
    if v_glob and c_glob and v_glob != c_glob:
        _check_policy_and_brace_drift(
            v_glob, c_glob, "global_instruction.txt", report.findings
        )

    # Agent pairs
    for pair in pair_agents(v_root, c_root, explicit_map):
        report.agent_pairs.append(
            {
                "merged_name": pair.merged_name,
                "voice_name": pair.voice_name,
                "chat_name": pair.chat_name,
            }
        )
        if pair.voice_dir is None:
            report.findings.append(
                Finding(
                    code="M_UNMATCHED_AGENT",
                    severity="INFO",
                    category="NEEDS_APPROVAL",
                    location=f"agents/{pair.chat_name}",
                    message=(
                        f"Agent '{pair.chat_name}' exists only in Chat app "
                        "(use --agent-map if renamed)."
                    ),
                )
            )
            c_inst = pair.chat_dir / "instruction.txt"  # type: ignore[operator]
            if c_inst.is_file():
                validate_instruction_text(
                    c_inst.read_text(encoding="utf-8"),
                    f"chat:agents/{pair.chat_name}/instruction.txt",
                    report.findings,
                )
            continue
        if pair.chat_dir is None:
            report.findings.append(
                Finding(
                    code="M_UNMATCHED_AGENT",
                    severity="INFO",
                    category="NEEDS_APPROVAL",
                    location=f"agents/{pair.voice_name}",
                    message=(
                        f"Agent '{pair.voice_name}' exists only in Voice app "
                        "(use --agent-map if renamed)."
                    ),
                )
            )
            v_inst = pair.voice_dir / "instruction.txt"
            if v_inst.is_file():
                validate_instruction_text(
                    v_inst.read_text(encoding="utf-8"),
                    f"voice:agents/{pair.voice_name}/instruction.txt",
                    report.findings,
                )
            continue

        # Matched pair: instructions + callbacks
        v_inst_p = pair.voice_dir / "instruction.txt"
        c_inst_p = pair.chat_dir / "instruction.txt"
        v_text = (
            v_inst_p.read_text(encoding="utf-8") if v_inst_p.is_file() else ""
        )
        c_text = (
            c_inst_p.read_text(encoding="utf-8") if c_inst_p.is_file() else ""
        )
        if v_text:
            validate_instruction_text(
                v_text,
                f"voice:agents/{pair.voice_name}/instruction.txt",
                report.findings,
            )
        if c_text:
            validate_instruction_text(
                c_text,
                f"chat:agents/{pair.chat_name}/instruction.txt",
                report.findings,
            )
        if v_text and c_text:
            _check_policy_and_brace_drift(
                v_text,
                c_text,
                f"agents/{pair.merged_name}/instruction.txt",
                report.findings,
            )

        v_cbs = _discover_agent_callbacks(pair.voice_dir)
        c_cbs = _discover_agent_callbacks(pair.chat_dir)
        for key, cb_p in v_cbs.items():
            if key.startswith("before_agent_callbacks/"):
                _check_before_agent_guard(
                    cb_p,
                    f"voice:agents/{pair.voice_name}/{key}",
                    report.findings,
                )
        for key, cb_p in c_cbs.items():
            if key.startswith("before_agent_callbacks/"):
                _check_before_agent_guard(
                    cb_p,
                    f"chat:agents/{pair.chat_name}/{key}",
                    report.findings,
                )
        for shared_cb in sorted(set(v_cbs) & set(c_cbs)):
            if v_cbs[shared_cb].read_bytes() != c_cbs[shared_cb].read_bytes():
                report.findings.append(
                    Finding(
                        code="M_CALLBACK_CONFLICT",
                        severity="WARNING",
                        category="NEEDS_APPROVAL",
                        location=f"agents/{pair.merged_name}/{shared_cb}",
                        message=(
                            f"Callback '{shared_cb}' differs between Voice and "
                            "Chat."
                        ),
                        fix="Reconcile both callbacks manually into one file.",
                    )
                )

    return report


# ── Callback Scaffolder ──────────────────────────────────────────────────


def _render_modality_mask_callback(
    voice_only: list[str], text_only: list[str]
) -> str:
    """Build a C001/C002/C019/I025-compliant before_model_callback file."""
    v_tuple = ", ".join(repr(t) for t in sorted(voice_only))
    if len(voice_only) == 1:
        v_tuple += ","
    t_tuple = ", ".join(repr(t) for t in sorted(text_only))
    if len(text_only) == 1:
        t_tuple += ","
    return (
        '"""Auto-scaffolded modality tool mask callback."""\n\n'
        "from typing import Optional\n\n"
        f"VOICE_ONLY_TOOLS = ({v_tuple})\n"
        f"TEXT_ONLY_TOOLS = ({t_tuple})\n\n\n"
        "def _hide_disallowed_modality_tools(\n"
        "    callback_context: CallbackContext, llm_request: LlmRequest\n"
        ") -> None:\n"
        "    session = callback_context.get_variable('_session', {})\n"
        "    modality = (\n"
        "        session.get('modality', '').upper()\n"
        "        if isinstance(session, dict)\n"
        "        else ''\n"
        "    )\n"
        "    config = getattr(llm_request, 'config', None)\n"
        "    if config is None or not modality:\n"
        "        return\n"
        "    excluded = set(getattr(config, 'excluded_tools', None) or ())\n"
        "    if modality == 'TEXT':\n"
        "        to_hide = VOICE_ONLY_TOOLS\n"
        "    elif modality == 'VOICE':\n"
        "        to_hide = TEXT_ONLY_TOOLS\n"
        "    else:\n"
        "        return\n"
        "    for tool_name in to_hide:\n"
        "        if tool_name in excluded:\n"
        "            continue\n"
        "        if hasattr(config, 'hide_tool'):\n"
        "            config.hide_tool(tool_name)\n\n\n"
        "def before_model_callback(\n"
        "    callback_context: CallbackContext, llm_request: LlmRequest\n"
        ") -> Optional[LlmResponse]:\n"
        "    _hide_disallowed_modality_tools(callback_context, llm_request)\n"
        "    return None\n"
    )


# ── Merge Engine ─────────────────────────────────────────────────────────


def run_merge(
    voice_dir: Path,
    chat_dir: Path,
    output_dir: Path,
    *,
    tag_style: str = "ces-tags",
    report_dir: Path | None = None,
    explicit_map: dict[str, str] | None = None,
    overwrite: bool = False,
) -> AuditReport:
    """Merge sibling Voice and Chat apps into a brand-new `output_dir`."""
    v_root = resolve_app_root(voice_dir)
    c_root = resolve_app_root(chat_dir)
    out_resolved = output_dir.resolve()
    if out_resolved in (v_root.resolve(), c_root.resolve()):
        raise ValueError(
            "Refusing to overwrite a source app directory; pass a new "
            "--output-dir."
        )
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output directory already exists: {output_dir}. "
                "Pass --overwrite to replace it."
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rep_root = report_dir or output_dir.parent / f"{output_dir.name}_reports"
    rep_root.mkdir(parents=True, exist_ok=True)

    report = run_audit(v_root, c_root, explicit_map)
    v_app = _load_json(v_root / "app.json")
    c_app = _load_json(c_root / "app.json")

    # 1. Union `app.json` (prefer voice audio config; union variables safely)
    merged_vars: list[dict[str, Any]] = []
    v_vmap = {
        v["name"]: v
        for v in v_app.get("variableDeclarations", [])
        if isinstance(v, dict) and v.get("name")
    }
    c_vmap = {
        v["name"]: v
        for v in c_app.get("variableDeclarations", [])
        if isinstance(v, dict) and v.get("name")
    }
    for name in sorted(set(v_vmap) | set(c_vmap)):
        if name in RESERVED_VARIABLES:
            continue
        merged_vars.append(v_vmap.get(name) or c_vmap[name])

    pairs = pair_agents(v_root, c_root, explicit_map)
    v_to_merged = {p.voice_name: p.merged_name for p in pairs if p.voice_name}
    c_to_merged = {p.chat_name: p.merged_name for p in pairs if p.chat_name}

    merged_app = dict(v_app or c_app)
    if "rootAgent" in merged_app and merged_app["rootAgent"] in v_to_merged:
        merged_app["rootAgent"] = v_to_merged[merged_app["rootAgent"]]
    if merged_vars:
        merged_app["variableDeclarations"] = merged_vars
    v_glob_p = v_root / "global_instruction.txt"
    c_glob_p = c_root / "global_instruction.txt"
    has_glob = v_glob_p.is_file() or c_glob_p.is_file()
    if has_glob:
        merged_app["globalInstruction"] = "global_instruction.txt"
    (output_dir / "app.json").write_text(
        json.dumps(merged_app, indent=2) + "\n", encoding="utf-8"
    )

    # Copy environment.json if present (Voice baseline)
    for env_src in (v_root / "environment.json", c_root / "environment.json"):
        if env_src.is_file():
            shutil.copy2(env_src, output_dir / "environment.json")
            break

    # 2. Merge global_instruction.txt (if either app has one)
    if has_glob:
        vg = v_glob_p.read_text(encoding="utf-8") if v_glob_p.is_file() else ""
        cg = c_glob_p.read_text(encoding="utf-8") if c_glob_p.is_file() else ""
        merged_glob = merge_instructions(vg, cg, tag_style=tag_style)
        (output_dir / "global_instruction.txt").write_text(
            merged_glob, encoding="utf-8"
        )
        _check_neutral_verbs_in_shared(
            merged_glob, "global_instruction.txt", report.findings
        )
        report.token_stats.append(
            _build_token_stat("global_instruction.txt", vg, cg, merged_glob)
        )

    # 3. Copy unioned tools (stage diverged Chat conflicts under report dir)
    v_tools = _discover_tools(v_root)
    c_tools = _discover_tools(c_root)
    if v_tools or c_tools:
        out_tools = output_dir / "tools"
        out_tools.mkdir(exist_ok=True)
        for t_name in sorted(set(v_tools) | set(c_tools)):
            src = v_tools.get(t_name) or c_tools[t_name]
            shutil.copytree(src, out_tools / t_name)
            if t_name in report.tool_conflicts:
                conflict_dir = rep_root / "_conflicts" / "tools" / t_name
                shutil.copytree(
                    c_tools[t_name], conflict_dir, dirs_exist_ok=True
                )

    # 4. Copy toolsets & guardrails (union; report conflicts untouched)
    for folder in ("toolsets", "guardrails"):
        for src_root in (v_root, c_root):
            src_dir = src_root / folder
            if not src_dir.is_dir():
                continue
            dst_parent = output_dir / folder
            dst_parent.mkdir(exist_ok=True)
            for child in sorted(src_dir.iterdir()):
                if child.is_dir() and not (dst_parent / child.name).exists():
                    shutil.copytree(child, dst_parent / child.name)

    # 5. Merge agents
    agents_out = output_dir / "agents"
    agents_out.mkdir(exist_ok=True)
    for pair in pairs:
        a_out = agents_out / pair.merged_name
        a_out.mkdir(parents=True, exist_ok=True)

        v_cfg = (
            _load_json(pair.voice_dir / f"{pair.voice_name}.json")
            if pair.voice_dir and pair.voice_name
            else {}
        )
        c_cfg = (
            _load_json(pair.chat_dir / f"{pair.chat_name}.json")
            if pair.chat_dir and pair.chat_name
            else {}
        )
        v_inst_t = (
            (pair.voice_dir / "instruction.txt").read_text(encoding="utf-8")
            if pair.voice_dir and (pair.voice_dir / "instruction.txt").is_file()
            else ""
        )
        c_inst_t = (
            (pair.chat_dir / "instruction.txt").read_text(encoding="utf-8")
            if pair.chat_dir and (pair.chat_dir / "instruction.txt").is_file()
            else ""
        )

        # Merge instruction.txt
        if pair.voice_dir and pair.chat_dir:
            merged_inst = merge_instructions(
                v_inst_t, c_inst_t, tag_style=tag_style
            )
        elif pair.voice_dir:
            merged_inst = merge_instructions(v_inst_t, "", tag_style=tag_style)
        else:
            merged_inst = merge_instructions("", c_inst_t, tag_style=tag_style)

        # Auto-append explicit voice guard if TEXT-only widgets exist.
        v_widgets = set(_WIDGET_REF_RE.findall(v_inst_t))
        c_widgets = set(_WIDGET_REF_RE.findall(c_inst_t))
        text_only_widgets = sorted(c_widgets - v_widgets)
        if text_only_widgets:
            names_str = ", ".join(text_only_widgets)
            guard_note = (
                f"Note: visual widgets ({names_str}) are text-only — "
                "forbid calling or referencing widgets during VOICE calls."
            )
            report.findings.append(
                Finding(
                    code="M_TEXT_ONLY_WIDGET",
                    severity="WARNING",
                    category="NEEDS_APPROVAL",
                    location=f"agents/{pair.merged_name}/instruction.txt",
                    message=(
                        f"Text-only widget(s) detected: {text_only_widgets}. "
                        "Add an explicit prohibition inside the VOICE block."
                    ),
                    fix=guard_note,
                )
            )

        (a_out / "instruction.txt").write_text(merged_inst, encoding="utf-8")
        _check_neutral_verbs_in_shared(
            merged_inst,
            f"agents/{pair.merged_name}/instruction.txt",
            report.findings,
        )
        report.token_stats.append(
            _build_token_stat(
                f"agents/{pair.merged_name}/instruction.txt",
                v_inst_t,
                c_inst_t,
                merged_inst,
            )
        )

        # Copy existing callback directories (rewriting agent path refs later)
        for src_dir in (pair.voice_dir, pair.chat_dir):
            if src_dir is None:
                continue
            for cb_type in CALLBACK_DIRS:
                src_cb_dir = src_dir / cb_type
                if not src_cb_dir.is_dir():
                    continue
                for slot in sorted(src_cb_dir.iterdir()):
                    dst_slot = a_out / cb_type / slot.name
                    if slot.is_dir() and not dst_slot.exists():
                        shutil.copytree(slot, dst_slot)
                    elif slot.is_file() and not dst_slot.exists():
                        dst_slot.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(slot, dst_slot)
                    elif (
                        src_dir == pair.chat_dir
                        and slot.is_dir()
                        and (slot / "python_code.py").is_file()
                        and (dst_slot / "python_code.py").is_file()
                        and (slot / "python_code.py").read_bytes()
                        != (dst_slot / "python_code.py").read_bytes()
                    ):
                        c_save = (
                            rep_root
                            / "_conflicts"
                            / "agents"
                            / pair.merged_name
                            / cb_type
                            / slot.name
                        )
                        shutil.copytree(slot, c_save, dirs_exist_ok=True)

        # Merge agent config JSON + compute per-agent modality-only tools
        v_tool_list = list(v_cfg.get("tools") or [])
        c_tool_list = list(c_cfg.get("tools") or [])
        v_set, c_set = set(v_tool_list), set(c_tool_list)
        if not v_set and v_inst_t:
            v_set = set(_TOOL_REF_RE.findall(v_inst_t))
        if not c_set and c_inst_t:
            c_set = set(_TOOL_REF_RE.findall(c_inst_t))
        agent_v_only = sorted(v_set - c_set - {"end_session"})
        agent_t_only = sorted(c_set - v_set - {"end_session"})

        merged_cfg = dict(v_cfg or c_cfg)
        merged_cfg["name"] = pair.merged_name
        merged_cfg.setdefault("displayName", pair.merged_name)
        merged_cfg["instruction"] = f"agents/{pair.merged_name}/instruction.txt"

        union_tools = list(dict.fromkeys([*v_tool_list, *c_tool_list]))
        if union_tools:
            merged_cfg["tools"] = union_tools

        union_children: list[str] = []
        for ch in list(v_cfg.get("childAgents") or []):
            mapped = v_to_merged.get(ch, ch)
            if mapped not in union_children:
                union_children.append(mapped)
        for ch in list(c_cfg.get("childAgents") or []):
            mapped = c_to_merged.get(ch, ch)
            if mapped not in union_children:
                union_children.append(mapped)
        if union_children:
            merged_cfg["childAgents"] = union_children

        # Scaffold `before_model_callback` modality tool masking if needed
        existing_bm = list(merged_cfg.get("beforeModelCallbacks") or [])
        if agent_v_only or agent_t_only:
            mask_rel = (
                f"agents/{pair.merged_name}/before_model_callbacks/"
                "modality_mask/python_code.py"
            )
            mask_path = output_dir / mask_rel
            mask_path.parent.mkdir(parents=True, exist_ok=True)
            mask_path.write_text(
                _render_modality_mask_callback(agent_v_only, agent_t_only),
                encoding="utf-8",
            )
            existing_bm.append(
                {
                    "pythonCode": mask_rel,
                    "description": (
                        "Auto-generated modality tool mask using "
                        "_session.modality and config.hide_tool()."
                    ),
                }
            )
            merged_cfg["beforeModelCallbacks"] = existing_bm
            if len(existing_bm) > 1:
                report.findings.append(
                    Finding(
                        code="M_CONSOLIDATE_CALLBACKS",
                        severity="INFO",
                        category="NEEDS_APPROVAL",
                        location=f"agents/{pair.merged_name}/{pair.merged_name}.json",
                        message=(
                            f"Agent has {len(existing_bm)} "
                            "beforeModelCallbacks. "
                            "Consolidate into one Python file to avoid extra "
                            "per-callback AST compile latency."
                        ),
                        fix=(
                            "Move `_hide_disallowed_modality_tools` into your "
                            "primary `before_model_callback`."
                        ),
                    )
                )

        (a_out / f"{pair.merged_name}.json").write_text(
            json.dumps(merged_cfg, indent=2) + "\n", encoding="utf-8"
        )

    # 6. Dual deployment config templates (kept in report_dir, never pushed)
    dep_dir = rep_root / "deployment_templates"
    dep_dir.mkdir(exist_ok=True)
    app_display = str(merged_app.get("displayName") or output_dir.name)
    (dep_dir / "deployment_voice.template.json").write_text(
        json.dumps(
            {
                "displayName": f"{app_display} (VOICE)",
                "channelProfile": {
                    "channelType": "GOOGLE_TELEPHONY_PLATFORM",
                    "modality": "VOICE",
                },
                "note": (
                    "Template only. Review & create explicitly with "
                    "human approval."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (dep_dir / "deployment_text.template.json").write_text(
        json.dumps(
            {
                "displayName": f"{app_display} (TEXT / Web Widget)",
                "channelProfile": {
                    "channelType": "WEB_UI",
                    "modality": "TEXT",
                },
                "note": (
                    "Template only. Review & create explicitly with "
                    "human approval."
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 7. Write Markdown + JSON merge reports
    (rep_root / "merge_report.json").write_text(
        json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    (rep_root / "MERGE_REPORT.md").write_text(
        format_markdown_report(report, output_dir=output_dir),
        encoding="utf-8",
    )
    return report


def _build_token_stat(
    name: str, voice_text: str, chat_text: str, merged_text: str
) -> dict[str, Any]:
    v_rendered = render_for_modality(merged_text, "VOICE")
    c_rendered = render_for_modality(merged_text, "TEXT")
    return {
        "file": name,
        "voice_source_tokens": _approx_tokens(voice_text),
        "chat_source_tokens": _approx_tokens(chat_text),
        "combined_unmerged_tokens": (
            _approx_tokens(voice_text) + _approx_tokens(chat_text)
        ),
        "merged_template_tokens": _approx_tokens(merged_text),
        "rendered_voice_tokens": _approx_tokens(v_rendered),
        "rendered_text_tokens": _approx_tokens(c_rendered),
    }


# ── Verification Engine ──────────────────────────────────────────────────


def run_verify(
    merged_dir: Path,
    *,
    voice_dir: Path | None = None,
    chat_dir: Path | None = None,
    explicit_map: dict[str, str] | None = None,
) -> list[Finding]:
    """Verify tag balance, Jinja subset, TTS/markdown isolation, and parity."""
    m_root = resolve_app_root(merged_dir)
    v_root = resolve_app_root(voice_dir) if voice_dir else None
    c_root = resolve_app_root(chat_dir) if chat_dir else None
    findings: list[Finding] = []

    def _check_one(
        rel: str,
        merged_txt: str,
        expected_v: str | None,
        expected_c: str | None,
    ) -> None:
        validate_instruction_text(merged_txt, rel, findings)
        v_render = render_for_modality(merged_txt, "VOICE")
        c_render = render_for_modality(merged_txt, "TEXT")

        # Voice TTS audio tags MUST NOT bleed into TEXT render!
        tts_masked = _mask_exempt(c_render)
        for m in _COMPOSITE_AUDIO_TAG_RE.finditer(tts_masked):
            findings.append(
                Finding(
                    code="M_TTS_BLEED_TO_TEXT",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=rel,
                    line=_line_of(tts_masked, m.start()),
                    message=(
                        f"Voice audio tag '{m.group(0)}' remains visible when "
                        "rendered under TEXT modality."
                    ),
                    fix=(
                        "Wrap inline emotion/prosody tags inside "
                        "{@startModality_Voice}...{@endModality_Voice}."
                    ),
                )
            )

        # Markdown links should not bleed into VOICE render.
        voice_masked = _mask_exempt(v_render)
        for m in _MARKDOWN_LINK_RE.finditer(voice_masked):
            findings.append(
                Finding(
                    code="M_MARKDOWN_BLEED_TO_VOICE",
                    severity="WARNING",
                    category="NEEDS_APPROVAL",
                    location=rel,
                    line=_line_of(voice_masked, m.start()),
                    message=(
                        f"Markdown link '{m.group(0)[:40]}' remains visible "
                        "when rendered under VOICE modality."
                    ),
                    fix=(
                        "Wrap markdown links inside "
                        "{@startModality_Text}...{@endModality_Text}."
                    ),
                )
            )

        # Lossless render-equivalence checks against originals (if given)
        if expected_v is not None and _norm_lines(v_render) != _norm_lines(
            expected_v
        ):
            diff = "\n".join(
                difflib.unified_diff(
                    _norm_lines(expected_v),
                    _norm_lines(v_render),
                    fromfile=f"source_voice:{rel}",
                    tofile=f"rendered_voice:{rel}",
                    lineterm="",
                )
            )
            findings.append(
                Finding(
                    code="M_VOICE_RENDER_DIFF",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=rel,
                    message=(
                        f"Rendered VOICE prompt differs from source:\n{diff}"
                    ),
                    fix="Inspect diff and confirm divergence is intended.",
                )
            )
        if expected_c is not None and _norm_lines(c_render) != _norm_lines(
            expected_c
        ):
            diff = "\n".join(
                difflib.unified_diff(
                    _norm_lines(expected_c),
                    _norm_lines(c_render),
                    fromfile=f"source_chat:{rel}",
                    tofile=f"rendered_text:{rel}",
                    lineterm="",
                )
            )
            findings.append(
                Finding(
                    code="M_TEXT_RENDER_DIFF",
                    severity="ERROR",
                    category="NEEDS_APPROVAL",
                    location=rel,
                    message=(
                        f"Rendered TEXT prompt differs from source:\n{diff}"
                    ),
                    fix="Inspect diff and confirm divergence is intended.",
                )
            )

    # Check global_instruction.txt
    m_glob = m_root / "global_instruction.txt"
    if m_glob.is_file():
        ev = (
            (v_root / "global_instruction.txt").read_text(encoding="utf-8")
            if v_root and (v_root / "global_instruction.txt").is_file()
            else ("" if v_root else None)
        )
        ec = (
            (c_root / "global_instruction.txt").read_text(encoding="utf-8")
            if c_root and (c_root / "global_instruction.txt").is_file()
            else ("" if c_root else None)
        )
        _check_one(
            "global_instruction.txt",
            m_glob.read_text(encoding="utf-8"),
            ev,
            ec,
        )

    # Check each merged agent's instruction.txt
    explicit = explicit_map or {}
    pair_map: dict[str, AgentPair] = {}
    if v_root and c_root:
        for p in pair_agents(v_root, c_root, explicit):
            pair_map[p.merged_name] = p

    for name, a_dir in _discover_agents(m_root).items():
        inst_p = a_dir / "instruction.txt"
        if not inst_p.is_file():
            continue
        p_info = pair_map.get(name)
        ev = ec = None
        if p_info is not None:
            if (
                p_info.voice_dir
                and (p_info.voice_dir / "instruction.txt").is_file()
            ):
                ev = (p_info.voice_dir / "instruction.txt").read_text(
                    encoding="utf-8"
                )
            elif v_root is not None:
                ev = ""
            if (
                p_info.chat_dir
                and (p_info.chat_dir / "instruction.txt").is_file()
            ):
                ec = (p_info.chat_dir / "instruction.txt").read_text(
                    encoding="utf-8"
                )
            elif c_root is not None:
                ec = ""
        _check_one(
            f"agents/{name}/instruction.txt",
            inst_p.read_text(encoding="utf-8"),
            ev,
            ec,
        )

    return findings


def _norm_lines(text: str) -> list[str]:
    """Trim outer empty lines and normalize trailing whitespace."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


# ── Report Formatting ────────────────────────────────────────────────────


def format_markdown_report(
    report: AuditReport, *, output_dir: Path | None = None
) -> str:
    """Format a concise Markdown audit / merge report."""
    lines = [
        "# CXAS Omnichannel Merge Report",
        "",
        f"- **Voice source:** `{report.voice_root}`",
        f"- **Chat source:** `{report.chat_root}`",
    ]
    if output_dir is not None:
        lines.append(f"- **Merged output:** `{output_dir}`")
    lines.extend(
        [
            f"- **Declared variables (`app.json`):** "
            f"`{report.total_variables}` (limit `{VARIABLE_COUNT_LIMIT}`)",
            "",
            "## Agent Pairing",
            "",
            "| Merged Agent | Voice Source | Chat Source |",
            "| :--- | :--- | :--- |",
        ]
    )
    for pair in report.agent_pairs:
        lines.append(
            f"| `{pair['merged_name']}` | "
            f"{pair['voice_name'] or '_none_'} | "
            f"{pair['chat_name'] or '_none_'} |"
        )

    lines.extend(
        [
            "",
            "## Tool Classification",
            "",
            f"- **Shared (`{len(report.tools_shared)}`):** "
            + (", ".join(f"`{t}`" for t in report.tools_shared) or "_none_"),
            f"- **Voice-only (`{len(report.tools_voice_only)}`):** "
            + (
                ", ".join(f"`{t}`" for t in report.tools_voice_only) or "_none_"
            ),
            f"- **Text-only (`{len(report.tools_text_only)}`):** "
            + (", ".join(f"`{t}`" for t in report.tools_text_only) or "_none_"),
            f"- **Conflicts (`{len(report.tool_conflicts)}`):** "
            + (", ".join(f"`{t}`" for t in report.tool_conflicts) or "_none_"),
        ]
    )

    if report.token_stats:
        lines.extend(
            [
                "",
                "## Approximate Token Counts (~4 chars/token)",
                "",
                "| File | Voice Src | Chat Src | Merged Template | "
                "VOICE Render | TEXT Render |",
                "| :--- | :---: | :---: | :---: | :---: | :---: |",
            ]
        )
        for st in report.token_stats:
            lines.append(
                f"| `{st['file']}` | {st['voice_source_tokens']} | "
                f"{st['chat_source_tokens']} | "
                f"{st['merged_template_tokens']} | "
                f"{st['rendered_voice_tokens']} | "
                f"{st['rendered_text_tokens']} |"
            )

    lines.extend(["", f"## Findings ({len(report.findings)})", ""])
    if not report.findings:
        lines.append("No drift, conflicts, or hygiene issues detected.")
    else:
        lines.extend(
            [
                "| Sev | Code | Scope | Location | Message |",
                "| :---: | :--- | :---: | :--- | :--- |",
            ]
        )
        for f in report.findings:
            loc = f"{f.location}:{f.line}" if f.line else f.location
            msg = f.message.replace("\n", " ").replace("|", "\\|")
            lines.append(
                f"| `{f.severity}` | `{f.code}` | `{f.category}` | "
                f"`{loc}` | {msg} |"
            )

    lines.append("")
    return "\n".join(lines)


# ── CLI Entrypoint ───────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit, merge, and verify sibling Voice and Chat CXAS apps."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="Read-only audit & drift report.")
    p_audit.add_argument("--voice-dir", required=True, type=Path)
    p_audit.add_argument("--chat-dir", required=True, type=Path)
    p_audit.add_argument("--agent-map", action="append", default=[])
    p_audit.add_argument("--report-md", type=Path)
    p_audit.add_argument("--report-json", type=Path)

    p_merge = sub.add_parser(
        "merge", help="Merge into a new omnichannel output directory."
    )
    p_merge.add_argument("--voice-dir", required=True, type=Path)
    p_merge.add_argument("--chat-dir", required=True, type=Path)
    p_merge.add_argument("--output-dir", required=True, type=Path)
    p_merge.add_argument("--report-dir", type=Path)
    p_merge.add_argument(
        "--tag-style",
        choices=("ces-tags", "jinja"),
        default="ces-tags",
        help="Default: ces-tags ({@startModality_*}).",
    )
    p_merge.add_argument("--agent-map", action="append", default=[])
    p_merge.add_argument("--overwrite", action="store_true")

    p_ver = sub.add_parser(
        "verify", help="Verify render equivalence & CES template hygiene."
    )
    p_ver.add_argument("--merged-dir", required=True, type=Path)
    p_ver.add_argument("--voice-dir", type=Path)
    p_ver.add_argument("--chat-dir", type=Path)
    p_ver.add_argument("--agent-map", action="append", default=[])
    p_ver.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any ERROR findings remain.",
    )
    p_ver.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    explicit_map = _parse_agent_map(getattr(args, "agent_map", None))

    if args.command == "audit":
        report = run_audit(args.voice_dir, args.chat_dir, explicit_map)
        md = format_markdown_report(report)
        if args.report_md:
            args.report_md.parent.mkdir(parents=True, exist_ok=True)
            args.report_md.write_text(md, encoding="utf-8")
        if args.report_json:
            args.report_json.parent.mkdir(parents=True, exist_ok=True)
            args.report_json.write_text(
                json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
            )
        print(md)
        return 0

    if args.command == "merge":
        report = run_merge(
            args.voice_dir,
            args.chat_dir,
            args.output_dir,
            tag_style=args.tag_style,
            report_dir=args.report_dir,
            explicit_map=explicit_map,
            overwrite=args.overwrite,
        )
        print(format_markdown_report(report, output_dir=args.output_dir))
        return 0

    if args.command == "verify":
        findings = run_verify(
            args.merged_dir,
            voice_dir=args.voice_dir,
            chat_dir=args.chat_dir,
            explicit_map=explicit_map,
        )
        if args.json_output:
            print(json.dumps([asdict(f) for f in findings], indent=2))
        else:
            errors = [f for f in findings if f.severity == "ERROR"]
            warns = [f for f in findings if f.severity == "WARNING"]
            for f in findings:
                loc = f"{f.location}:{f.line}" if f.line else f.location
                print(f"  [{f.severity[0]}] {loc} [{f.code}] {f.message}")
            print(
                f"\nVerification: {len(errors)} error(s), "
                f"{len(warns)} warning(s)."
            )
        if args.strict and any(f.severity == "ERROR" for f in findings):
            return 1
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
