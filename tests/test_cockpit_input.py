"""UX2-R1 (CU-UX-2): cockpit_input — intent translation minimal skeleton.

AC18 Intent Boundary (DESIGN LOCK v1.1 §3/§11): the translator is a pure
string classifier. It imports no engine module and no Textual — Composer
Intent stays a vocabulary, never Execution Semantics; the sole arbiter of
any submitted text remains the ControlBoundary.
"""
import ast
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cockpit_input


class ClassifySubmitTests(unittest.TestCase):
    """R1 real subset: funnel pre-start = TASK, post-Start = STEER."""

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

    def test_r2_intents_are_vocabulary_only_never_classified(self):
        # R1 真子集：classify_submit 的返回域恰 {TASK, STEER}——
        # REVISION/COMMAND 是 R2 语义位，绝不提前实现。
        for pre in (True, False):
            for text in ("/revise it", "/pause", "/help", "/target prompt"):
                self.assertIn(
                    cockpit_input.classify_submit(funnel_pre_start=pre, text=text),
                    {cockpit_input.INTENT_TASK, cockpit_input.INTENT_STEER})


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
        # 只暴露四个意图词 + 分类函数；无类、无状态、无副作用。
        exported = set(cockpit_input.__all__)
        self.assertEqual(
            exported,
            {"INTENT_TASK", "INTENT_STEER", "INTENT_REVISION",
             "INTENT_COMMAND", "classify_submit"})
        self.assertTrue(callable(cockpit_input.classify_submit))


if __name__ == "__main__":
    unittest.main()
