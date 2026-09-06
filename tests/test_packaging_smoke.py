"""P1-U4: packaging / release UX smoke — real build + isolated venv install.

§8 纪律：全程离线（build --no-isolation、pip --no-deps --no-index），
临时目录安装（绝不污染当前 Python 环境）。§9/§10 的 qualify / run /
--observe 冒烟由 driver 脚本在隔离 venv 内以安装态包驱动，全部用
确定性 TEST fake（零 REAL invocation、零网络、零凭据）。

skip / fail 语义（既有 OfflineExampleTests 先例）：python-build 缺席
时诚实 skip；build / venv / 安装失败诚实 fail —— 本机工具链是本轮
产品的组成部分，不是可选装饰。
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib is 3.11+ stdlib
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[1]

_BUILD_TIMEOUT = 600
_VENV_TIMEOUT = 300
_DRIVER_TIMEOUT = 300


def _run(command, *, timeout, cwd=None, env=None):
    return subprocess.run(
        [str(part) for part in command], capture_output=True, text=True,
        timeout=timeout, cwd=str(cwd) if cwd else None, env=env,
        encoding="utf-8", errors="replace")


def _package_version():
    text = (REPO / "dual-agent-development" / "scripts" / "__init__.py"
            ).read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise AssertionError("package shim must define __version__")
    return match.group(1)


# --- §9/§10 driver：隔离 venv 内以安装态包驱动的确定性 fake 冒烟 ---
# 刻意用平铺模块名（shim 暴露）：真实默认 qualifier 路径
# （real_validation_executor 等旧件）内部正是平铺 import —— fixture 与
# 产品路径同 module graph，冒烟才忠实。
_DRIVER = r'''"""P1-U4 packaging smoke driver (TEST FIXTURE — offline, deterministic).

Runs inside the throwaway venv against the INSTALLED dual_agent package.
FakeFamilyAdapter / qualifier mirror tests/test_host_entry.py: they answer
by request-role semantics and never invoke a real runtime, the network,
or credentials.
"""
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import dual_agent  # noqa: F401 — activates the flat-import shim
from dual_agent import host_entry

from candidate_validation import (
    CandidateValidationResult,
    CandidateValidationStatus,
    GateResult,
    GateVerdict,
    ValidationGate,
)
from external_runtime import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    RuntimeDiscovery,
    RuntimeProfile,
)
from runtime_status import AuthenticationState, ReasonCode

CAPS_ALL = ("architecture", "coding", "review", "testing")

ARCH_P = {"task_id": "t", "role": "architect", "goal": ["g"], "constraints": ["c"],
          "architecture": ["a"], "interfaces": [{}], "implementation_steps": [{}],
          "acceptance_criteria": ["ac"], "risks": [{}]}
IMPL_P = {"task_id": "t", "role": "coder", "changed_files": ["f"],
          "implementation_summary": "s", "implementation_details": ["d"],
          "assumptions": [], "unresolved_items": [], "test_requirements": ["tr"]}
TEST_P = {"task_id": "t", "role": "tester", "tests_run": ["x"], "tests_passed": ["x"],
          "tests_failed": [], "failures": [], "coverage_or_validation": [],
          "remaining_risks": []}
REVIEW_P = {"task_id": "t", "role": "reviewer", "status": "PASS", "findings": [],
            "severity": [], "affected_files": [], "required_changes": [],
            "acceptance_criteria_status": []}


def _check(label, condition):
    if not condition:
        raise SystemExit("driver assertion failed: " + label)


class FakeFamilyAdapter:
    """TEST FIXTURE — mirrors tests/test_host_entry.py FakeFamilyAdapter."""

    def __init__(self, runtime_id, provider_id):
        self.profile = RuntimeProfile(
            agent_id="coding-agent", runtime=runtime_id,
            provider=provider_id, model=None, role="coder",
            capabilities=frozenset())
        self.runtime_id = runtime_id
        self.provider_id = provider_id

    def discover(self):
        return RuntimeDiscovery(self.runtime_id, True, "1.0", None, frozenset())

    def check_authentication(self):
        from runtime_health import AuthenticationCheck
        return AuthenticationCheck(AuthenticationState.AUTHENTICATED, "oauth")

    def check_provider_model(self):
        from runtime_health import ProviderModelCheck
        return ProviderModelCheck(self.provider_id, None, True, ReasonCode.NONE)

    def minimal_health_check(self, timeout_seconds):
        from runtime_health import MinimalHealthCheck
        return MinimalHealthCheck(True, ReasonCode.NONE, output_class="exact_ok")

    def invoke(self, request):
        if request.agent_id == self._identity_json():
            return self._packet(IMPL_P)
        for role, packet in (("architect", ARCH_P), ("coder", IMPL_P),
                             ("tester", TEST_P), ("reviewer", REVIEW_P)):
            if request.agent_id == role or request.agent_id.endswith(
                    ',"' + role + '"]'):
                return self._packet(packet)
        return InvocationResult(
            InvocationStatus.SUCCESS, output="OK",
            trace=self._trace(request))

    def _identity_json(self):
        return json.dumps([self.runtime_id, self.provider_id, None, "default"],
                          separators=(",", ":"))

    @staticmethod
    def _packet(packet):
        return InvocationResult(
            InvocationStatus.SUCCESS, output=json.dumps(packet),
            trace=InvocationTrace(
                invocation_id="inv-f", task_id="t", agent_id="a",
                runtime="rt-a", provider=None, model=None, role=None,
                status=InvocationStatus.SUCCESS, started_at=0.0,
                finished_at=0.0, duration_ms=1, exit_code=0,
                input_tokens="unknown", output_tokens="unknown", error=None))

    @staticmethod
    def _trace(request):
        return InvocationTrace(
            invocation_id="inv-f2", task_id=request.task_id,
            agent_id=request.agent_id, runtime="rt-a", provider=None,
            model=None, role=request.role, status=InvocationStatus.SUCCESS,
            started_at=0.0, finished_at=0.0, duration_ms=1, exit_code=0,
            input_tokens="unknown", output_tokens="unknown", error=None)


def evidence_for(runtime_id, provider_id):
    return CandidateValidationResult(
        identity=(runtime_id, provider_id, None, "default"),
        status=CandidateValidationStatus.VERIFIED,
        gates_passed=frozenset(ValidationGate),
        gate_results=tuple(GateResult(g, GateVerdict.PASS)
                           for g in ValidationGate),
        block_reason=None, failure_point=None, experiment_id="exp-f",
        executed_at=0.0, validated_capabilities=CAPS_ALL,
        evidence={}, provenance="REAL")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        adapter_a = FakeFamilyAdapter("rt-a", "provider-a")
        adapter_b = FakeFamilyAdapter("rt-b", "provider-b")
        factories = (lambda: adapter_a, lambda: adapter_b)

        def qualifier(instance):
            return evidence_for(instance.runtime_id, instance.provider_id)

        # Case A: run without evidence -> honest exit 2 + machine JSON
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_entry.main(["run", "redesign architecture across modules"],
                                   factories=factories, base_dir=str(base))
        _check("case A exit 2", code == 2)
        payload = json.loads(out.getvalue())
        _check("case A reason", payload["reason"] == "NO_EVIDENCE_NO_QUALIFIER")
        _check("case A human stderr", "qualify" in err.getvalue())

        # Case C: qualify with the TEST qualifier -> exit 0 + persistence
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_entry.main(["qualify"], factories=factories,
                                   qualifier=qualifier, base_dir=str(base))
        _check("qualify exit 0", code == 0)
        summary = json.loads(out.getvalue())
        _check("qualify saved 2", len(summary.get("saved_files", [])) == 2)
        _check("files on disk", len(list(base.glob("*.json"))) == 2)

        # Case B: run reads persisted evidence (no injection) -> SUCCESS
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_entry.main(["run", "--mode", "on",
                                    "redesign architecture across modules"],
                                   factories=factories, base_dir=str(base))
        _check("case B exit 0", code == 0)
        payload = json.loads(out.getvalue())
        _check("case B success", payload["status"] == "SUCCESS")

        # §10: --observe keeps stdout a single machine JSON line
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = host_entry.main(["run", "--observe", "--mode", "on",
                                    "redesign architecture across modules"],
                                   factories=factories, base_dir=str(base))
        _check("observe exit 0", code == 0)
        _check("observe single stdout line", out.getvalue().count("\n") == 1)
        json.loads(out.getvalue())
        _check("observe DECISION on stderr", "DECISION" in err.getvalue())
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
'''


class PackagingSmokeTests(unittest.TestCase):
    """§8 打包冒烟：build → 隔离 venv → 安装 → 公开入口面逐项证明。"""

    @classmethod
    def setUpClass(cls):
        try:
            import build  # noqa: F401
        except ModuleNotFoundError:
            raise unittest.SkipTest(
                "python-build not installed: offline packaging smoke unavailable")
        # 只清理本测试自己制造的构建副产物（源树 egg-info / build 目录）；
        # 预先存在的目录绝不触碰。
        cls._preexisting = {
            str(path): path.exists()
            for path in (REPO / "dual_agent_development.egg-info",
                         REPO / "build")}
        try:
            cls._tmp = tempfile.TemporaryDirectory()
            cls.root = Path(cls._tmp.name)
            cls.dist = cls.root / "dist"
            for flag in ("--sdist", "--wheel"):
                built = _run([sys.executable, "-m", "build", flag,
                              "--no-isolation", "--outdir", cls.dist],
                             cwd=REPO, timeout=_BUILD_TIMEOUT)
                if built.returncode != 0:
                    raise AssertionError(
                        f"python -m build {flag} failed "
                        f"(rc={built.returncode}):\n{built.stderr[-3000:]}")
            wheels = sorted(cls.dist.glob("*.whl"))
            sdists = sorted(cls.dist.glob("*.tar.gz"))
            if len(wheels) != 1 or len(sdists) != 1:
                raise AssertionError(
                    f"unexpected dist artifacts: {wheels} {sdists}")
            cls.wheel, cls.sdist = wheels[0], sdists[0]

            cls.venv = cls.root / "venv"
            created = _run([sys.executable, "-m", "venv", cls.venv],
                           timeout=_VENV_TIMEOUT)
            if created.returncode != 0:
                raise AssertionError(
                    f"venv creation failed:\n{created.stderr[-2000:]}")
            scripts_dir = "Scripts" if os.name == "nt" else "bin"
            cls.venv_python = cls.venv / scripts_dir / (
                "python.exe" if os.name == "nt" else "python")
            cls.console = cls.venv / scripts_dir / (
                "dual-agent.exe" if os.name == "nt" else "dual-agent")
            installed = _run([cls.venv_python, "-m", "pip", "install",
                              "--no-deps", "--no-index", cls.wheel],
                             timeout=_VENV_TIMEOUT)
            if installed.returncode != 0:
                raise AssertionError(
                    f"pip install failed:\n{installed.stderr[-3000:]}")
            cls.driver = cls.root / "driver.py"
            cls.driver.write_text(_DRIVER, encoding="utf-8")
        except BaseException:
            cls._cleanup_build_side_effects()
            raise

    @classmethod
    def _cleanup_build_side_effects(cls):
        for raw_path, existed in cls.__dict__.get("_preexisting", {}).items():
            if existed:
                continue
            def _onerror(func, target, _exc_info):
                # Windows 只读/句柄延迟：chmod 后重试一次，仍失败则放弃
                #（egg-info/build 均为 .gitignore 覆盖的构建残渣）。
                try:
                    os.chmod(target, 0o777)
                    func(target)
                except OSError:
                    pass
            shutil.rmtree(raw_path, onerror=_onerror)

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_build_side_effects()
        tmp = cls.__dict__.get("_tmp")
        if tmp is not None:
            tmp.cleanup()

    # --- §5 版本契约：单一 truth，安装面逐处对齐 ---

    def test_pyproject_version_is_dynamic_single_truth(self):
        data = tomllib.loads(
            (REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", data["project"].get("dynamic", []),
                      "pyproject must declare version as dynamic")
        self.assertNotIn(
            "version", data["project"],
            "version literal in [project] would shadow the dynamic attr")
        self.assertEqual(
            data["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "dual_agent.__version__")

    def test_installed_distribution_version_matches_package_truth(self):
        version = _package_version()
        meta = _run([self.venv_python, "-c",
                     "import importlib.metadata as m; "
                     "print(m.version('dual-agent-development'))"], timeout=60)
        self.assertEqual(meta.returncode, 0, meta.stderr[-500:])
        self.assertEqual(meta.stdout.strip(), version)
        shim = _run([self.venv_python, "-c",
                     "import dual_agent; print(dual_agent.__version__)"],
                    timeout=60)
        self.assertEqual(shim.returncode, 0, shim.stderr[-500:])
        self.assertEqual(shim.stdout.strip(), version)

    def test_console_script_version_flag(self):
        version = _package_version()
        done = _run([self.console, "--version"], timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(done.stdout.strip(), f"dual-agent {version}")
        self.assertEqual(done.stderr, "")

    # --- §6 帮助契约：产品面可发现 ---

    def test_console_script_help_documents_product_surface(self):
        done = _run([self.console, "--help"], timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(done.stderr, "")
        lowered = done.stdout.lower()
        for concept in ("run", "qualify", "--observe", "--version"):
            self.assertIn(concept, lowered)
        self.assertIn("never automatically qualifies", lowered)

    def test_module_entry_matches_console_entry(self):
        version = _package_version()
        done = _run([self.venv_python, "-m", "dual_agent", "--version"],
                    cwd=self.root, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(done.stdout.strip(), f"dual-agent {version}")

    # --- §13 打包完整性：只装产品，绝不装测试/秘密/开发残渣 ---

    def test_wheel_contents_are_product_only(self):
        version = _package_version()
        names = zipfile.ZipFile(self.wheel).namelist()
        # data-files 落在 wheel 的 data 类别路径 <dist>.data/data/ 下，
        # 安装时 pip 把它们重定位到 sys.prefix/share/...。
        allowed_prefixes = (
            "dual_agent/",
            f"dual_agent_development-{version}.data/data/",
            f"dual_agent_development-{version}.dist-info/")
        for name in names:
            self.assertTrue(name.startswith(allowed_prefixes), name)
            lowered_name = name.lower()
            self.assertFalse(lowered_name.startswith("tests/"), name)
            for marker in (".mimosa", ".env", "credential", "__pycache__",
                           ".tmp", ".git/"):
                self.assertNotIn(marker, lowered_name, name)
        share_prefix = f"dual_agent_development-{version}.data/data/"
        for required in ("dual_agent/__init__.py", "dual_agent/__main__.py",
                         "dual_agent/cli.py", "dual_agent/host_entry.py",
                         "dual_agent/evidence_store.py",
                         share_prefix + "share/dual-agent/skill/SKILL.md",
                         share_prefix
                         + "share/dual-agent/skill/templates/architecture-packet.json"):
            self.assertIn(required, names)

    def test_sdist_contents_are_product_only(self):
        with tarfile.open(self.sdist) as archive:
            names = archive.getnames()
        joined = "\n".join(names).lower()
        for marker in ("/tests/", ".mimosa", "__pycache__", ".git/",
                       "credential"):
            self.assertNotIn(marker, joined)
        for marker in (".env", ".tmp"):
            for name in names:
                self.assertNotIn(marker, name.lower())
        self.assertTrue(
            any(name.endswith("pyproject.toml") for name in names),
            "sdist must carry pyproject.toml")
        self.assertTrue(
            any(name.endswith("README.md") for name in names),
            "sdist must carry README.md")
        self.assertTrue(
            any(name.endswith("scripts/host_entry.py") for name in names),
            "sdist must carry the package source")

    # --- §9/§10 安装态产品冒烟：fake 驱动 qualify/run/--observe ---

    def test_installed_offline_cli_smoke_cases(self):
        env = {k: v for k, v in os.environ.items()
               if k != "RUN_REAL_PROVIDER_TESTS"}
        done = _run([self.venv_python, self.driver], cwd=self.root,
                    timeout=_DRIVER_TIMEOUT, env=env)
        self.assertEqual(done.returncode, 0,
                         done.stdout[-2000:] + done.stderr[-2000:])
        self.assertIn("SMOKE_OK", done.stdout)


if __name__ == "__main__":
    unittest.main()
