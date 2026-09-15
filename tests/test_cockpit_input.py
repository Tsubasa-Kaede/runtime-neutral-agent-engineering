"""UX2-R2 (CU-UX-2): cockpit_input — intent translation + slash registry.

AC18 Intent Boundary (DESIGN LOCK v1.1 §3/§11): the translator is a pure
string classifier + closed data registry. It imports no engine module and
no Textual — Composer Intent stays a vocabulary, never Execution
Semantics; the sole arbiter of any submitted text remains the
ControlBoundary. Unknown slash commands stay an honest no-op.
"""
import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cockpit_input


class ClassifySubmitTests(unittest.TestCase):
    """四类意图分类矩阵（R2 全集；R1 的 TASK/STEER 真子集保持）。"""

    def test_pre_start_submit_is_task(self):
        self.assertEqual(
            cockpit_input.classify_submit(funnel_pre_start=True, text="write tests"),
            cockpit_input.INTENT_TASK)

    def test_post_start_submit_is_steer(self):
        self.assertEqual(
            cockpit_input.classify_submit(funnel_pre_start=False, text="write tests"),
            cockpit_input.INTENT_STEER)

    def test_blank_text_is_caller_concern_not_special_cased(self):
        # 设计明言：空白 no-op 由调用方处理，本函数不做空白特判。
        self.assertEqual(
            cockpit_input.classify_submit(funnel_pre_start=False, text=""),
            cockpit_input.INTENT_STEER)

    def test_intent_set_is_locked_four(self):
        self.assertEqual(
            {cockpit_input.INTENT_TASK, cockpit_input.INTENT_STEER,
             cockpit_input.INTENT_REVISION, cockpit_input.INTENT_COMMAND},
            {"TASK", "STEER", "REVISION", "COMMAND"})

    def test_slash_text_is_command_post_start(self):
        for text in ("/pause", "/target prompt", "/x", "/"):
            self.assertEqual(
                cockpit_input.classify_submit(
                    funnel_pre_start=False, text=text),
                cockpit_input.INTENT_COMMAND, text)

    def test_recall_flag_is_revision(self):
        self.assertEqual(
            cockpit_input.classify_submit(
                funnel_pre_start=False, text="fix api",
                revision_recall=True),
            cockpit_input.INTENT_REVISION)

    def test_slash_wins_over_recall(self):
        # 斜杠优先于召回态（命令域与非命令域互斥）。
        self.assertEqual(
            cockpit_input.classify_submit(
                funnel_pre_start=False, text="/pause",
                revision_recall=True),
            cockpit_input.INTENT_COMMAND)

    def test_funnel_overrides_everything_including_slash(self):
        # 漏斗判定冻结：Start 前斜杠亦是任务文本域，不是命令。
        for text in ("/pause", "/help", "write tests"):
            for recall in (False, True):
                self.assertEqual(
                    cockpit_input.classify_submit(
                        funnel_pre_start=True, text=text,
                        revision_recall=recall),
                    cockpit_input.INTENT_TASK)


class SlashRegistryTests(unittest.TestCase):
    """封闭注册表恰九条；kind 四值；数据纯度（无 callable）。"""

    def test_registry_is_closed_set_of_nine(self):
        self.assertEqual(
            set(cockpit_input.SLASH_REGISTRY),
            {"pause", "resume", "abort", "trace", "help",
             "lang", "context", "clear", "target"})

    def test_kinds_are_closed_set(self):
        kinds = {spec["kind"] for spec in
                 cockpit_input.SLASH_REGISTRY.values()}
        self.assertEqual(
            kinds, {"dispatch", "confirm", "screen", "local"})

    def test_dispatch_specs_carry_protocol_kinds(self):
        # dispatch_kind = 既有注入回调协议词（PAUSE/RESUME）——零新词。
        self.assertEqual(
            cockpit_input.SLASH_REGISTRY["pause"]["dispatch_kind"], "PAUSE")
        self.assertEqual(
            cockpit_input.SLASH_REGISTRY["resume"]["dispatch_kind"], "RESUME")
        for name, spec in cockpit_input.SLASH_REGISTRY.items():
            if spec["kind"] != "dispatch":
                self.assertIsNone(spec["dispatch_kind"], name)

    def test_every_command_has_nonempty_help(self):
        for name, spec in cockpit_input.SLASH_REGISTRY.items():
            self.assertTrue(spec["help"].strip(), name)


class ParseSlashTests(unittest.TestCase):

    def test_parse_plain_command(self):
        self.assertEqual(
            cockpit_input.parse_slash("/pause"), ("pause", ""))

    def test_parse_command_with_argument(self):
        # 首空格分词；参数=其余原文去首尾空白（内部空白保留）。
        self.assertEqual(
            cockpit_input.parse_slash("/target  extra  words "),
            ("target", "extra  words"))

    def test_parse_non_slash_returns_none(self):
        self.assertEqual(
            cockpit_input.parse_slash("hello"), (None, ""))
        self.assertEqual(cockpit_input.parse_slash(""), (None, ""))

    def test_parse_bare_slash_is_unknown_name(self):
        # "/" → 名 ""——注册表必 miss → unknown 诚实路径。
        self.assertEqual(cockpit_input.parse_slash("/"), ("", ""))


class SlashCandidatesTests(unittest.TestCase):

    def test_prefix_filters_in_registry_order(self):
        self.assertEqual(
            cockpit_input.slash_candidates("t"), ("trace", "target"))

    def test_empty_prefix_is_full_set(self):
        self.assertEqual(
            len(cockpit_input.slash_candidates("")), 9)

    def test_no_match_is_empty_tuple(self):
        self.assertEqual(cockpit_input.slash_candidates("zz"), ())

    def test_full_word_matches_only_itself(self):
        self.assertEqual(
            cockpit_input.slash_candidates("pause"), ("pause",))


class SlashHelpTests(unittest.TestCase):

    def test_help_lines_cover_all_nine(self):
        lines = cockpit_input.slash_help_lines()
        self.assertEqual(len(lines), 9)
        for name in cockpit_input.SLASH_REGISTRY:
            self.assertTrue(
                any(line.startswith(f"/{name} — ") for line in lines),
                name)

    def test_help_is_pure_data_no_state(self):
        # EN 冻结面：无 locale 参数、无副作用——调用两次同一结果。
        self.assertEqual(cockpit_input.slash_help_lines(),
                         cockpit_input.slash_help_lines())


class IntentBoundaryGuardTests(unittest.TestCase):
    """AC18：零引擎 import、零 Textual import（AST 级断言源码级纯度）。"""

    _BANNED = ("dual_agent", "textual", "rich",
               "control_boundary", "sequential_pipeline", "cockpit_entry")

    def test_source_imports_no_engine_no_textual(self):
        src = (SCRIPTS / "cockpit_input.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                self.assertNotIn(root, self._BANNED,
                                 f"cockpit_input must not import {name!r}")

    def test_module_namespace_holds_only_vocabulary(self):
        # 只暴露意图词 + 注册表 + 纯函数；无类、无状态、无副作用。
        exported = set(cockpit_input.__all__)
        self.assertEqual(
            exported,
            {"INTENT_TASK", "INTENT_STEER", "INTENT_REVISION",
             "INTENT_COMMAND", "SLASH_REGISTRY", "classify_submit",
             "parse_slash", "slash_candidates", "slash_help_lines"})
        self.assertTrue(callable(cockpit_input.classify_submit))
        self.assertTrue(callable(cockpit_input.parse_slash))
        self.assertTrue(callable(cockpit_input.slash_candidates))
        self.assertTrue(callable(cockpit_input.slash_help_lines))


if __name__ == "__main__":
    unittest.main()
