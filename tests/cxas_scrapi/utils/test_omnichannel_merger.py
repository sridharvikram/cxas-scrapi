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

"""Unit tests for the cxas-omnichannel-merger skill CLI script."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SKILL_DIR = _REPO_ROOT / ".agents" / "skills" / "cxas-omnichannel-merger"
_SCRIPT_PATH = _SKILL_DIR / "scripts" / "omnichannel_merger.py"
_SAMPLE_VOICE = _SKILL_DIR / "resources" / "sample-pair" / "voice_app"
_SAMPLE_CHAT = _SKILL_DIR / "resources" / "sample-pair" / "chat_app"


def _load_merger() -> Any:
    name = "cxas_omnichannel_merger_script"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_sample_pair_merge_and_verify_ces_tags(tmp_path: Path) -> None:
    m = _load_merger()
    out_dir = tmp_path / "merged_app"
    rep_dir = tmp_path / "reports"

    report = m.run_merge(
        _SAMPLE_VOICE,
        _SAMPLE_CHAT,
        out_dir,
        tag_style="ces-tags",
        report_dir=rep_dir,
    )

    assert "lookup_order" in report.tools_shared
    assert "send_dtmf_prompt" in report.tools_voice_only
    assert "render_order_card" in report.tools_text_only
    assert report.tool_conflicts == []

    # Intentional policy drift (30 days vs 45 days) MUST be caught.
    codes = {f.code for f in report.findings}
    assert "M_POLICY_DRIFT" in codes
    assert "M_TEXT_ONLY_WIDGET" in codes

    # Top-level XML tags kept static, modality tags wrap only inside.
    merged_inst = (
        out_dir / "agents" / "support_agent" / "instruction.txt"
    ).read_text(encoding="utf-8")
    assert merged_inst.startswith("<role>\n")
    assert "{@startModality_Voice}" in merged_inst
    assert "{@startModality_Text}" in merged_inst

    # Modality tool-masking callback scaffolded with typing import & _session.
    mask_cb = (
        out_dir
        / "agents"
        / "support_agent"
        / "before_model_callbacks"
        / "modality_mask"
        / "python_code.py"
    )
    assert mask_cb.is_file()
    cb_code = mask_cb.read_text(encoding="utf-8")
    assert "from typing import Optional" in cb_code
    assert "'send_dtmf_prompt'" in cb_code
    assert "'render_order_card'" in cb_code
    assert "config.hide_tool" in cb_code

    # Dual deployment templates and reports created in report_dir.
    assert (rep_dir / "MERGE_REPORT.md").is_file()
    assert (rep_dir / "merge_report.json").is_file()
    assert (
        rep_dir / "deployment_templates" / "deployment_voice.template.json"
    ).is_file()
    assert (
        rep_dir / "deployment_templates" / "deployment_text.template.json"
    ).is_file()

    # Exact lossless render equivalence verification passes with 0 errors!
    verify_findings = m.run_verify(
        out_dir, voice_dir=_SAMPLE_VOICE, chat_dir=_SAMPLE_CHAT
    )
    errors = [f for f in verify_findings if f.severity == "ERROR"]
    assert errors == []


def test_sample_pair_merge_and_verify_jinja_style(tmp_path: Path) -> None:
    m = _load_merger()
    out_dir = tmp_path / "merged_jinja"
    rep_dir = tmp_path / "reports_jinja"

    m.run_merge(
        _SAMPLE_VOICE,
        _SAMPLE_CHAT,
        out_dir,
        tag_style="jinja",
        report_dir=rep_dir,
    )
    merged_inst = (
        out_dir / "agents" / "support_agent" / "instruction.txt"
    ).read_text(encoding="utf-8")
    assert "{{% if _session.modality == 'VOICE' %}}" in merged_inst
    assert "{{% endif %}}" in merged_inst

    verify_findings = m.run_verify(
        out_dir, voice_dir=_SAMPLE_VOICE, chat_dir=_SAMPLE_CHAT
    )
    assert [f for f in verify_findings if f.severity == "ERROR"] == []


def test_plain_text_without_xml_merges_and_renders_losslessly() -> None:
    m = _load_merger()
    voice = "Shared line.\nRead aloud digit by digit.\nEnd line.\n"
    chat = "Shared line.\nDisplay in bold with markdown.\nEnd line.\n"

    for style in ("ces-tags", "jinja"):
        merged = m.merge_instructions(voice, chat, tag_style=style)
        assert m.render_for_modality(merged, "VOICE") == voice
        assert m.render_for_modality(merged, "TEXT") == chat


def test_validate_catches_all_modular_and_best_practice_gotchas() -> None:
    m = _load_merger()
    bad = (
        "{# unstripped comment #}\n"
        "{% set x = 1 %}\n"
        "{% if 'a' not in roles and delta > -10 and bad-var == 'y' %}\n"
        "  {{ tier | lower }} {{ upper(tier) }}\n"
        "  {{ preferred | default('Guest', boolean_eval=true) }}\n"
        "  {{ customer['first_name'] }} ${legacy_var}\n"
        "  {% for item in items %}{{ loop.index }}{% endfor %}\n"
        "{% endif %}\n"
        "{@startChannel_Telephony}nope{@endChannel_Telephony}\n"
        "{@startModality_Voice}\n<persona>wrapped</persona>\n"
        "{@endModality_Text}\n"
        "</prosody> <slow> [warm]\n"
        "raw trailing braces}}\n"
    )
    findings: list[Any] = []
    m.validate_instruction_text(bad, "instruction.txt", findings)
    codes = {f.code for f in findings}
    expected = {
        "M_JINJA_COMMENT",
        "M_UNSUPPORTED_TAG",
        "M_NOT_IN",
        "M_NEG_LITERAL",
        "M_HYPHEN_VAR",
        "M_UNSUPPORTED_FILTER",
        "M_UPPER_CALLABLE",
        "M_FILTER_KWARG",
        "M_DICT_SUBSCRIPT",
        "M_LEGACY_DOLLAR_VAR",
        "M_UNSUPPORTED_LOOP_ATTR",
        "M_INVALID_CES_TAG",
        "M_MISMATCHED_CLOSER",
        "M_LEGACY_TTS_TAG",
        "M_TRAILING_BRACES",
    }
    assert expected.issubset(codes)


def test_audit_detects_reserved_vars_type_conflict_brace_drift_and_cb_guard(
    tmp_path: Path,
) -> None:
    m = _load_merger()
    v_dir = tmp_path / "voice"
    c_dir = tmp_path / "chat"
    for d in (v_dir, c_dir):
        (d / "agents" / "root_agent").mkdir(parents=True)
        (d / "tools" / "shared_tool").mkdir(parents=True)

    (v_dir / "app.json").write_text(
        json.dumps(
            {
                "displayName": "Demo",
                "rootAgent": "root_agent",
                "variableDeclarations": [
                    {"name": "_session", "schema": {"type": "OBJECT"}},
                    {"name": "user:tier", "schema": {"type": "STRING"}},
                    {"name": "caller-id", "schema": {"type": "STRING"}},
                    {"name": "order_id", "schema": {"type": "STRING"}},
                ],
            }
        )
    )
    (c_dir / "app.json").write_text(
        json.dumps(
            {
                "displayName": "Demo",
                "rootAgent": "root_agent",
                "variableDeclarations": [
                    {"name": "order_id", "schema": {"type": "NUMBER"}},
                ],
            }
        )
    )
    (v_dir / "tools" / "shared_tool" / "tool.json").write_text('{"v": 1}')
    (c_dir / "tools" / "shared_tool" / "tool.json").write_text('{"v": 2}')

    (v_dir / "agents" / "root_agent" / "instruction.txt").write_text(
        "Order is {order_id}. Window is 30 days.\n"
    )
    (c_dir / "agents" / "root_agent" / "instruction.txt").write_text(
        "Order is {{ order_id }}. Window is 60 days.\n"
    )
    bad_cb = (
        v_dir
        / "agents"
        / "root_agent"
        / "before_agent_callbacks"
        / "init"
        / "python_code.py"
    )
    bad_cb.parent.mkdir(parents=True)
    bad_cb.write_text(
        "def before_agent_callback(callback_context):\n"
        "    callback_context.state['retry_count'] = 0\n"
    )

    report = m.run_audit(v_dir, c_dir)
    codes = {f.code for f in report.findings}
    assert "M_RESERVED_VAR_DECLARED" in codes
    assert "M_COLON_VAR_NAME" in codes
    assert "M_HYPHEN_VAR_DECL" in codes
    assert "M_VAR_TYPE_CONFLICT" in codes
    assert "M_TOOL_CONFLICT" in codes
    assert "M_POLICY_DRIFT" in codes
    assert "M_BRACE_DRIFT" in codes
    assert "M_MISSING_TURN_GUARD" in codes

    # Merging must omit reserved `_session` from app.json variableDeclarations.
    out_dir = tmp_path / "out"
    m.run_merge(v_dir, c_dir, out_dir, report_dir=tmp_path / "rep")
    merged_app = json.loads((out_dir / "app.json").read_text(encoding="utf-8"))
    decl_names = [v["name"] for v in merged_app["variableDeclarations"]]
    assert "_session" not in decl_names
    assert (
        tmp_path / "rep" / "_conflicts" / "tools" / "shared_tool" / "tool.json"
    ).is_file()


def test_verify_flags_tts_tag_bleed_into_text_and_refuses_in_place_overwrite(
    tmp_path: Path,
) -> None:
    m = _load_merger()
    app = tmp_path / "app"
    (app / "agents" / "a").mkdir(parents=True)
    (app / "app.json").write_text('{"displayName": "A", "rootAgent": "a"}')
    (app / "agents" / "a" / "instruction.txt").write_text(
        "<role>\n<warm>Unwrapped voice audio tag leaks into TEXT!\n"
        "Check [docs](https://example.com) unwrapped leaks into VOICE.\n"
        "</role>\n"
    )
    findings = m.run_verify(app)
    codes = {f.code for f in findings}
    assert "M_TTS_BLEED_TO_TEXT" in codes
    assert "M_MARKDOWN_BLEED_TO_VOICE" in codes

    with pytest.raises(ValueError, match="overwrite a source"):
        m.run_merge(_SAMPLE_VOICE, _SAMPLE_CHAT, _SAMPLE_VOICE)


def test_auto_pairing_strips_voice_chat_suffixes_and_supports_agent_map(
    tmp_path: Path,
) -> None:
    m = _load_merger()
    v_dir = tmp_path / "v"
    c_dir = tmp_path / "c"
    (v_dir / "agents" / "orders_voice").mkdir(parents=True)
    (v_dir / "agents" / "alpha").mkdir(parents=True)
    (c_dir / "agents" / "orders_chat").mkdir(parents=True)
    (c_dir / "agents" / "beta").mkdir(parents=True)
    (v_dir / "app.json").write_text("{}")
    (c_dir / "app.json").write_text("{}")

    pairs = m.pair_agents(v_dir, c_dir, {"alpha": "beta"})
    by_merged = {p.merged_name: (p.voice_name, p.chat_name) for p in pairs}
    assert by_merged["orders"] == ("orders_voice", "orders_chat")
    assert by_merged["alpha"] == ("alpha", "beta")


def test_cli_verify_strict_exit_code(tmp_path: Path) -> None:
    m = _load_merger()
    out_dir = tmp_path / "merged"
    rep_dir = tmp_path / "rep"
    rc = m.main(
        [
            "merge",
            "--voice-dir",
            str(_SAMPLE_VOICE),
            "--chat-dir",
            str(_SAMPLE_CHAT),
            "--output-dir",
            str(out_dir),
            "--report-dir",
            str(rep_dir),
        ]
    )
    assert rc == 0
    rc_verify = m.main(
        [
            "verify",
            "--merged-dir",
            str(out_dir),
            "--voice-dir",
            str(_SAMPLE_VOICE),
            "--chat-dir",
            str(_SAMPLE_CHAT),
            "--strict",
        ]
    )
    assert rc_verify == 0
