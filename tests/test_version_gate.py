"""RELEASE-CI: 版本一致性门（scripts/version_gate.py）的离线确定性测试。

本文件先于实现存在（RED：scripts/version_gate.py 缺席时 collection 失败）。

背景（2.2.1 发布后的 CI 故障）：publish.yml / publish-testpypi.yml 的门禁
内联脚本访问 ``pyproject["project"]["version"]``，而本仓库 pyproject 声明
``dynamic = ["version"]`` —— 该键不存在，裸 KeyError 使 tag 推送失败。

锁定的行为（TDD 六项）：
1. static version fixture：tag == [project].version 即通过（既有行为）
2. dynamic version fixture：tag/expected == dual_agent.__version__ 即通过，
   全程绝不读取 [project]["version"]
3. 当前真实 pyproject.toml：dynamic 单一真相 + 门禁对真实 checkout 通过
4. tag v2.2.1 == dual_agent.__version__ 2.2.1：PASS（本 checkout 的发布对）
5. mismatch：必须 FAIL，且错误信息同时携带两个数字
6. malformed / missing version source：给出明确错误（VersionGateError），
   绝不允许裸 KeyError（结构上 VersionGateError 也不是 KeyError 子类）

版本来源纪律：门禁从 pyproject 自身的 package-dir / dynamic.attr 映射解析
出 ``__init__.py``，并以显式文件路径 import 当前 checkout —— 绝不解析到
site-packages 里的已安装副本，绝不经 pip install 掩盖，绝不复制第二套
版本真相。
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "version_gate.py"
REAL_INIT = REPO / "dual-agent-development" / "scripts" / "__init__.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("version_gate_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _real_version():
    match = re.search(r'__version__\s*=\s*"([^"]+)"',
                      REAL_INIT.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"checkout version truth not readable: {REAL_INIT}")
    return match.group(1)


DYNAMIC_PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "fixture-dynamic"
dynamic = ["version"]

[tool.setuptools.package-dir]
fixture_pkg = "src/fixture_pkg"

[tool.setuptools.dynamic]
version = { attr = "fixture_pkg.__version__" }
"""

DYNAMIC_NO_ATTR_PYPROJECT = """\
[project]
name = "fixture-dynamic-no-attr"
dynamic = ["version"]

[tool.setuptools.package-dir]
fixture_pkg = "src/fixture_pkg"
"""

STATIC_PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "fixture-static"
version = "9.9.9"
"""

STATIC_WITH_ATTR_PYPROJECT = """\
[project]
name = "fixture-static-with-attr"
version = "9.9.9"

[tool.setuptools.package-dir]
fixture_pkg = "src/fixture_pkg"

[tool.setuptools.dynamic]
version = { attr = "fixture_pkg.__version__" }
"""

NO_VERSION_PYPROJECT = """\
[project]
name = "fixture-unversioned"
"""


def _make_project(root, pyproject_text, pkg_init_text=None,
                  pkg_dir="src/fixture_pkg"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    if pkg_init_text is not None:
        (root / pkg_dir).mkdir(parents=True, exist_ok=True)
        (root / pkg_dir / "__init__.py").write_text(pkg_init_text,
                                                    encoding="utf-8")
    return root


class GateErrorContractTests(unittest.TestCase):
    """失败面契约：明确错误，绝不裸 KeyError。"""

    def test_gate_error_is_not_keyerror(self):
        self.assertNotIsInstance(gate.VersionGateError("x"), KeyError)
        self.assertFalse(issubclass(gate.VersionGateError, KeyError))

    def test_missing_pyproject_file_is_clear_error(self):
        with self.assertRaises(gate.VersionGateError) as ctx:
            gate.check(tag="v1.0.0", pyproject_path="does/not/exist.toml")
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_toml_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "pyproject.toml"
            broken.write_text("[project\nname = ", encoding="utf-8")
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v1.0.0", pyproject_path=broken)
            self.assertIn("TOML", str(ctx.exception))

    def test_no_version_declaration_is_clear_error_not_keyerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), NO_VERSION_PYPROJECT)
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v1.0.0", pyproject_path=root / "pyproject.toml")
            message = str(ctx.exception)
            self.assertIn("version", message)
            self.assertNotIsInstance(ctx.exception, KeyError)

    def test_dynamic_without_attr_mapping_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_NO_ATTR_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertIn("version.attr", str(ctx.exception))

    def test_attr_target_without_constant_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text="# no version constant here\n")
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            message = str(ctx.exception)
            self.assertIn("__version__", message)
            self.assertIn("__init__.py", message)

    def test_mapped_package_dir_missing_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT)  # no src/fixture_pkg
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertIn("version source not found", str(ctx.exception))

    def test_malformed_tag_is_rejected_with_clear_error(self):
        for bad in ("release-2.2.1", "v2.2", "2.2.1", "v"):
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag=bad)
            self.assertIn(bad, str(ctx.exception))

    def test_malformed_expected_is_rejected_with_clear_error(self):
        with self.assertRaises(gate.VersionGateError) as ctx:
            gate.check(expected="not-a-version")
        self.assertIn("not-a-version", str(ctx.exception))

    def test_selector_is_exactly_one(self):
        with self.assertRaises(gate.VersionGateError):
            gate.check()
        with self.assertRaises(gate.VersionGateError):
            gate.check(tag="v1.0.0", expected="1.0.0")


class StaticVersionTests(unittest.TestCase):
    """TDD #1: static version fixture —— 既有行为保持。"""

    def test_static_tag_match_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), STATIC_PYPROJECT)
            result = gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertEqual(result.version, "9.9.9")
            self.assertEqual(result.name, "fixture-static")

    def test_static_tag_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), STATIC_PYPROJECT)
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v9.9.8", pyproject_path=root / "pyproject.toml")
            self.assertIn("9.9.9", str(ctx.exception))

    def test_static_with_matching_package_truth_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), STATIC_WITH_ATTR_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            result = gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertEqual(result.version, "9.9.9")

    def test_static_with_divergent_package_truth_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), STATIC_WITH_ATTR_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.8"\n')
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertIn("fixture_pkg.__version__=9.9.8", str(ctx.exception))


class DynamicVersionTests(unittest.TestCase):
    """TDD #2: dynamic version fixture —— KeyError 回归的正面用例。"""

    def test_dynamic_tag_match_passes_without_reading_project_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            result = gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertEqual(result.version, "9.9.9")
            self.assertEqual(result.name, "fixture-dynamic")

    def test_dynamic_expected_match_passes(self):
        # publish-testpypi.yml 的入参形态：裸版本号
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            result = gate.check(expected="9.9.9",
                                pyproject_path=root / "pyproject.toml")
            self.assertEqual(result.version, "9.9.9")

    def test_dynamic_resolves_from_checkout_not_installed_copy(self):
        # fixture 的 __version__ 是 9.9.9 —— 任何已安装 dual_agent（2.2.1 或
        # 其他）绝不能参与判定；解析必须命中显式源码路径。
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            result = gate.check(tag="v9.9.9", pyproject_path=root / "pyproject.toml")
            self.assertEqual(result.version, "9.9.9")


class MismatchTests(unittest.TestCase):
    """TDD #5: mismatch 必须 FAIL，信息完整。"""

    def test_dynamic_tag_mismatch_fails_with_both_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            with self.assertRaises(gate.VersionGateError) as ctx:
                gate.check(tag="v9.9.8", pyproject_path=root / "pyproject.toml")
            message = str(ctx.exception)
            self.assertIn("mismatch", message)
            self.assertIn("9.9.9", message)
            self.assertIn("9.9.8", message)

    def test_dynamic_expected_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            with self.assertRaises(gate.VersionGateError):
                gate.check(expected="9.9.8", pyproject_path=root / "pyproject.toml")


class RealRepositoryTests(unittest.TestCase):
    """TDD #3 + #4: 当前真实 pyproject.toml 与真实发布对 v2.2.1 == 2.2.1。"""

    def test_real_pyproject_is_dynamic_single_truth(self):
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("version", data["project"].get("dynamic", []),
                      "premise: real pyproject declares version dynamic")
        self.assertNotIn("version", data["project"])
        self.assertEqual(
            data["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "dual_agent.__version__")
        self.assertEqual(
            data["tool"]["setuptools"]["package-dir"]["dual_agent"],
            "dual-agent-development/scripts")

    def test_real_repo_gate_passes_for_current_release_tag(self):
        truth = _real_version()
        result = gate.check(tag="v" + truth, pyproject_path=REPO / "pyproject.toml")
        self.assertEqual(result.version, truth)
        self.assertEqual(result.name, "dual-agent-development")

    def test_real_release_pair_v2_2_1_passes_end_to_end(self):
        # TDD #4 的字面场景：本 checkout 的发布对 tag v2.2.1 == 2.2.1。
        # 发行版前进后自动 skip，避免钉死历史版本。
        truth = _real_version()
        if truth != "2.2.1":
            self.skipTest(f"release moved on: dual_agent.__version__={truth}")
        proc = subprocess.run(
            [sys.executable, str(GATE), "--tag", "v2.2.1",
             "--pyproject", str(REPO / "pyproject.toml")],
            capture_output=True, text=True, timeout=120, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0,
                         f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}")
        self.assertIn("version gate OK", proc.stdout)

    def test_real_repo_mismatch_fails(self):
        truth = _real_version()
        if not re.fullmatch(r"\d+\.\d+\.\d+", truth):
            self.skipTest(f"non plain release version: {truth}")
        bumped = truth[:-1] + str(int(truth[-1]) + 1)
        with self.assertRaises(gate.VersionGateError) as ctx:
            gate.check(tag="v" + bumped, pyproject_path=REPO / "pyproject.toml")
        message = str(ctx.exception)
        self.assertIn("mismatch", message)
        self.assertIn(truth, message)
        self.assertIn(bumped, message)


class CommandLineTests(unittest.TestCase):
    """workflow 调用形态的端到端契约（exit code / stdout / stderr）。"""

    def test_cli_default_pyproject_relative_to_cwd_passes(self):
        # 与 publish.yml 完全一致的调用形态：cwd=checkout 根，默认 ./pyproject.toml
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            proc = subprocess.run(
                [sys.executable, str(GATE), "--tag", "v9.9.9"],
                capture_output=True, text=True, timeout=120, cwd=str(root))
            self.assertEqual(proc.returncode, 0,
                             f"stderr:\n{proc.stderr}")
            self.assertIn("version gate OK", proc.stdout)
            self.assertIn("9.9.9", proc.stdout)

    def test_cli_mismatch_exits_nonzero_with_reason_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp), DYNAMIC_PYPROJECT,
                                 pkg_init_text='__version__ = "9.9.9"\n')
            proc = subprocess.run(
                [sys.executable, str(GATE), "--tag", "v9.9.8"],
                capture_output=True, text=True, timeout=120, cwd=str(root))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("FAILED", proc.stderr)
            self.assertIn("mismatch", proc.stderr)
            self.assertNotIsInstance(
                gate.VersionGateError("probe"), KeyError)

    def test_cli_requires_exactly_one_selector(self):
        no_selector = subprocess.run(
            [sys.executable, str(GATE)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(no_selector.returncode, 2)
        both = subprocess.run(
            [sys.executable, str(GATE), "--tag", "v1.0.0", "--expected", "1.0.0"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(both.returncode, 2)


if __name__ == "__main__":
    unittest.main()
