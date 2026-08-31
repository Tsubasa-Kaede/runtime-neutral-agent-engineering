"""Phase 10H-I: defensive runtime usage capture — the adapter contract.

V2's accounting contract (task_budget docstring, born in 10G-B) reserves
token fields for *observed* values only: adapters parse their OWN CLI
output format, fill the trace when usage is genuinely present, and leave
"unknown" everywhere else. This file locks that contract offline:

  valid usage   -> exact integer capture on the trace
  missing usage -> "unknown"
  malformed     -> "unknown" (never 0, never an estimate)
  parser failure NEVER fails the invocation itself

REAL availability of usage is UNKNOWN until a REAL-authorized run says
otherwise; every test here uses synthetic CLI stdout fixtures.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from external_runtime import InvocationStatus


def run_adapter(module_name, adapter_factory, request, stdout):
    """Invoke an adapter against a fake process emitting `stdout`."""
    import importlib
    module = importlib.import_module(module_name)
    FakeProcess = type(
        "FakeProcess", (), {
            "stdout": stdout, "stderr": "", "returncode": 0, "pid": 1,
            "killed": False, "calls": [],
            "communicate": lambda self, input=None, timeout=None: (self.stdout, self.stderr),
            "kill": lambda self: None,
        })
    with patch(f"{module_name}.subprocess.Popen", return_value=FakeProcess()):
        return adapter_factory(module).invoke(request)


class SharedUsageContractMixin:
    """One contract, three adapters: each provides fixtures for its own
    CLI output format; the assertions are identical (runtime-neutral)."""

    # subclasses set these
    module_name = None
    adapter_factory = None          # (module) -> adapter
    request = None                  # one ExternalAgentRequest
    stdout_with_usage = None        # CLI output carrying valid usage
    expected_input = None
    expected_output = None
    stdout_without_usage = None     # CLI output with no usage keys
    stdout_malformed_usage = None   # usage keys with junk values

    def invoke(self, stdout):
        return run_adapter(self.module_name, self.adapter_factory,
                           self.request, stdout)

    # -- Invariant 1: valid usage -> exact capture ---------------------

    def test_valid_usage_captured_exactly(self):
        result = self.invoke(self.stdout_with_usage)
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, self.expected_input)
        self.assertEqual(result.trace.output_tokens, self.expected_output)

    # -- Invariant 2: missing usage -> unknown --------------------------

    def test_missing_usage_stays_unknown(self):
        result = self.invoke(self.stdout_without_usage)
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- Invariant 3: malformed usage -> unknown, never 0 ---------------

    def test_malformed_usage_stays_unknown_not_zero(self):
        result = self.invoke(self.stdout_malformed_usage)
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertNotEqual(result.trace.input_tokens, 0)
        self.assertNotEqual(result.trace.output_tokens, 0)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")

    # -- Parser must never break the invocation -------------------------

    def test_unexpected_output_still_succeeds_with_unknown(self):
        result = self.invoke("not json at all\n")
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")


class ClaudeUsageCaptureTests(SharedUsageContractMixin, unittest.TestCase):
    module_name = "claude_code_adapter"

    @classmethod
    def setUpClass(cls):
        from claude_code_adapter import ClaudeCodeAdapter
        from external_runtime import ExternalAgentRequest, RuntimeProfile
        profile = RuntimeProfile("coding-agent", "claude-cli", "anthropic",
                                 None, "coder", frozenset())
        cls.adapter_factory = staticmethod(lambda module: ClaudeCodeAdapter(
            profile=profile, executable="claude"))
        cls.request = ExternalAgentRequest(
            task_id="t1", prompt="p", agent_id="coding-agent", role="coder",
            timeout_seconds=3)
        # claude --print --output-format json 封装：result 文本 + usage 键
        cls.stdout_with_usage = json.dumps({
            "result": "ok",
            "usage": {"input_tokens": 120, "output_tokens": 80},
        }) + "\n"
        cls.expected_input = 120
        cls.expected_output = 80
        cls.stdout_without_usage = json.dumps({"result": "ok"}) + "\n"
        cls.stdout_malformed_usage = json.dumps({
            "result": "ok",
            "usage": {"input_tokens": "lots", "output_tokens": -5},
        }) + "\n"


class PiUsageCaptureTests(SharedUsageContractMixin, unittest.TestCase):
    module_name = "pi_adapter"

    @classmethod
    def setUpClass(cls):
        from pi_adapter import PiAdapter
        from external_runtime import ExternalAgentRequest, RuntimeProfile
        profile = RuntimeProfile("coding-agent", "pi-cli", "deepseek",
                                 None, "coder", frozenset())
        cls.adapter_factory = staticmethod(lambda module: PiAdapter(
            profile=profile, executable="pi"))
        cls.request = ExternalAgentRequest(
            task_id="t1", prompt="p", agent_id="coding-agent", role="coder",
            timeout_seconds=3)

        def agent_end_event(usage=None):
            messages = [
                {"role": "user", "content": [{"type": "text", "text": "q"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            ]
            event = {"type": "agent_end", "messages": messages}
            if usage is not None:
                event["usage"] = usage
            return event

        def stream(*events):
            lines = [json.dumps({"type": "session", "version": 3,
                                 "id": "u", "cwd": "/tmp"})]
            lines.extend(json.dumps(e) for e in events)
            return "\n".join(lines) + "\n"

        cls.stdout_with_usage = stream(agent_end_event(
            {"input_tokens": 200, "output_tokens": 90}))
        cls.expected_input = 200
        cls.expected_output = 90
        cls.stdout_without_usage = stream(agent_end_event())
        cls.stdout_malformed_usage = stream(agent_end_event(
            {"input_tokens": "??", "output_tokens": None}))


class CodexUsageCaptureTests(SharedUsageContractMixin, unittest.TestCase):
    module_name = "codex_adapter"

    @classmethod
    def setUpClass(cls):
        from codex_adapter import CodexAdapter
        from external_runtime import ExternalAgentRequest, RuntimeProfile
        profile = RuntimeProfile("coding-agent", "codex-cli", "openai",
                                 None, "coder", frozenset())
        cls.adapter_factory = staticmethod(lambda module: CodexAdapter(
            profile=profile, executable="codex"))
        cls.request = ExternalAgentRequest(
            task_id="t1", prompt="p", agent_id="coding-agent", role="coder",
            timeout_seconds=3)
        # codex exec 无 JSON 封装（原始 stdout）：usage 不可观测 → unknown
        cls.stdout_with_usage = "ok\n"  # codex: no structured usage surface
        cls.expected_input = "unknown"  # contract: codex stays unknown today
        cls.expected_output = "unknown"
        cls.stdout_without_usage = "ok\n"
        cls.stdout_malformed_usage = "ok\n"

    def test_valid_usage_captured_exactly(self):
        # codex CLI 的 exec 输出没有机器可读 usage 面；即使 stdout
        # 恰好含 token 字样也不得捕风捉影 —— 诚实 unknown。
        result = self.invoke("ok\n token_usage=123")
        self.assertIs(result.status, InvocationStatus.SUCCESS)
        self.assertEqual(result.trace.input_tokens, "unknown")
        self.assertEqual(result.trace.output_tokens, "unknown")


if __name__ == "__main__":
    unittest.main()
