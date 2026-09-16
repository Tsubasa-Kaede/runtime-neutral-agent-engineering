"""CU-COCKPIT-1 P1: COMPOSE projection — deterministic pure functions.

Mandate: the compose screen is a read-only projection over the verified
pool listing + local selection state. Zero IO, zero events, zero engine
vocabulary; role words are domain words (never translated, never
re-defined here — COMPOSITION_ROLES derives from the same frozen
DEFAULT_ROLE_TEMPLATES table that composition_core.ROLE_VOCABULARY
derives from).
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from cockpit_projection import (  # noqa: E402
    COMPOSITION_ROLES,
    DEFAULT_ROLE_TEMPLATES,
    compose_keys_hint,
    compose_participant_lines,
    compose_pool_lines,
    compose_screen_lines,
    funnel_preview_lines,
)


def _entry(runtime_id, provider_id):
    return SimpleNamespace(runtime_id=runtime_id, provider_id=provider_id)


def _pool():
    return tuple(_entry(rt, f"prov-{rt[-1]}") for rt in ("rt-a", "rt-b", "rt-c"))


def _resolved_composition(bindings):
    """ResolvedComposition duck（呈现面形状：bindings 四字段行）。"""
    return SimpleNamespace(
        bindings=tuple(
            SimpleNamespace(role=role, runtime_id=runtime,
                            provider_id=provider,
                            canonical_runtime_identity=(
                                runtime, provider, None, "f"))
            for role, runtime, provider in bindings),
        member_ids=("member-1", "member-2"), groups=(),
        steps=tuple((role, runtime) for role, runtime, _ in bindings))


class CompositionRolesTests(unittest.TestCase):
    def test_roles_derive_from_frozen_templates(self):
        expected = tuple(sorted(
            {role for template in DEFAULT_ROLE_TEMPLATES.values()
             for role in template}))
        self.assertEqual(COMPOSITION_ROLES, expected)
        self.assertEqual(COMPOSITION_ROLES,
                         ("architect", "coder", "reviewer", "tester"))

    def test_default_role_template_position(self):
        # 与 default 组合同表同位次（N 钳 2-4）——经 entry 子集池
        # 回填实现（唯一指派真源复用），本表为对照真源
        self.assertEqual(DEFAULT_ROLE_TEMPLATES[2],
                         ("architect", "coder"))
        self.assertEqual(DEFAULT_ROLE_TEMPLATES[4][2], "tester")


class ComposePoolLinesTests(unittest.TestCase):
    def test_checkbox_cursor_and_provider_display(self):
        lines = compose_pool_lines(
            _pool(), selected_ids=("rt-a",), cursor_index=1)
        self.assertEqual(
            lines,
            ("  [x] rt-a · prov-a",
             "▶ [ ] rt-b · prov-b",
             "  [ ] rt-c · prov-c"))

    def test_empty_pool_is_honest_single_line(self):
        lines = compose_pool_lines((), locale="en")
        self.assertEqual(lines, ("no VERIFIED runtimes",))
        self.assertEqual(compose_pool_lines((), locale="zh"),
                         ("无已验证运行时",))

    def test_ascii_fallback(self):
        lines = compose_pool_lines(
            (_entry("rt-a", "prov-a"),), cursor_index=0, ascii_only=True)
        self.assertEqual(lines[0], "> [ ] rt-a · prov-a")


class ComposeParticipantLinesTests(unittest.TestCase):
    def test_member_rows_with_role_and_cursor(self):
        lines = compose_participant_lines(
            ("rt-a", "rt-b"),
            {"rt-a": "architect", "rt-b": "coder"},
            participant_index=1)
        self.assertEqual(
            lines,
            ("  member-1  ARCHITECT ← rt-a",
             "▶ member-2  CODER ← rt-b"))

    def test_duplicate_roles_render_as_declared(self):
        lines = compose_participant_lines(
            ("rt-a", "rt-b"),
            {"rt-a": "architect", "rt-b": "architect"})
        self.assertEqual(lines[0].split()[1], "member-1")
        self.assertIn("ARCHITECT", lines[1])

    def test_unassigned_role_renders_honest_dash(self):
        lines = compose_participant_lines(("rt-a",), {})
        self.assertEqual(lines, ("▶ member-1  — ← rt-a",))

    def test_no_selection_yields_no_rows(self):
        self.assertEqual(compose_participant_lines((), {}), ())


class ComposeScreenLinesTests(unittest.TestCase):
    def _screen(self, **overrides):
        values = dict(
            version_text="2.7.0", task_text="demo task", entries=_pool(),
            selected_ids=("rt-a", "rt-b"), cursor_index=0,
            roles={"rt-a": "architect", "rt-b": "coder"},
            participant_index=0, preview_composition=None,
            message_lines=(), width=100, ascii_only=False, locale="en")
        values.update(overrides)
        return compose_screen_lines(**values)

    def test_assembly_order_sections_and_hint(self):
        lines = self._screen()
        text = "\n".join(lines)
        self.assertIn("dual-agent cockpit · 2.7.0", lines[0])
        self.assertIn("TASK: demo task", text)
        self.assertLess(lines.index("── runtimes"),
                        lines.index("▶ [x] rt-a · prov-a"))
        self.assertLess(lines.index("── participants"),
                        lines.index("▶ member-1  ARCHITECT ← rt-a"))
        # 键提示恒末行
        self.assertEqual(lines[-1], compose_keys_hint(locale="en"))

    def test_preview_block_rendered_only_when_composition_given(self):
        without = self._screen()
        self.assertNotIn("architect", "\n".join(without))
        with_preview = self._screen(
            preview_composition=_resolved_composition(
                (("architect", "rt-a", "prov-a"),
                 ("coder", "rt-b", "prov-b"))))
        text = "\n".join(with_preview)
        self.assertIn("collaboration plan", text)
        self.assertIn("architect  ← rt-a · prov-a", text)

    def test_message_lines_rendered_before_hint(self):
        lines = self._screen(message_lines=("INVALID_MEMBER_COUNT: boom",))
        self.assertIn("INVALID_MEMBER_COUNT: boom", lines)
        self.assertNotEqual(lines[-1], "INVALID_MEMBER_COUNT: boom")

    def test_locale_switches_labels_not_domain_words(self):
        text = "\n".join(self._screen(locale="zh"))
        self.assertIn("运行时", text)
        self.assertIn("参与者", text)
        # role/runtime 为 domain 词，绝不翻译
        self.assertIn("ARCHITECT", text)
        self.assertIn("rt-a", text)

    def test_narrow_width_truncates_every_line(self):
        for width in (60, 80, 120, 160):
            lines = self._screen(
                width=width,
                preview_composition=_resolved_composition(
                    (("architect", "rt-a", "prov-a"),
                     ("coder", "rt-b", "prov-b"))),
                message_lines=("x" * 200,))
            self.assertTrue(lines)
            for line in lines:
                self.assertLessEqual(len(line), width)

    def test_ascii_only(self):
        text = "\n".join(self._screen(ascii_only=True))
        self.assertNotIn("←", text)
        self.assertNotIn("▶", text)
        self.assertNotIn("──", text)


class ComposeKeysHintTests(unittest.TestCase):
    def test_bilingual_and_ascii(self):
        en = compose_keys_hint(locale="en")
        zh = compose_keys_hint(locale="zh")
        self.assertIn("space select", en)
        self.assertIn("勾选", zh)
        self.assertNotIn("←", compose_keys_hint(locale="en",
                                                ascii_only=True))


class FunnelPreviewHeaderCompatTests(unittest.TestCase):
    def test_default_header_bytes_unchanged(self):
        lines = funnel_preview_lines(_resolved_composition(
            (("architect", "rt-a", "prov-a"),)))
        self.assertEqual(lines[0], "Collaboration plan (default)")


if __name__ == "__main__":
    unittest.main()
