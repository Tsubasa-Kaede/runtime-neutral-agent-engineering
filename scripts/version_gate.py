"""Version consistency gate for the release workflows.

Called by .github/workflows/publish.yml (tag push) and
.github/workflows/publish-testpypi.yml (workflow_dispatch):

    python scripts/version_gate.py --tag v2.2.1
    python scripts/version_gate.py --expected 2.2.1

Single version truth: ``dual_agent.__version__``. pyproject.toml declares the
distribution version dynamically — ``[project] dynamic = ["version"]`` plus
``[tool.setuptools.dynamic] version.attr = "dual_agent.__version__"`` — so
``[project]["version"]`` does not exist and is never read here (reading it
was the KeyError that broke the v2.2.1 tag build).

What the gate proves:

    normalized tag ("v2.2.1" -> "2.2.1") or --expected input
        == dual_agent.__version__, imported from THIS checkout

The version source is resolved from pyproject's own mapping
([tool.setuptools.package-dir] + [tool.setuptools.dynamic] version.attr) and
imported by explicit file path, so an installed (possibly stale) dual_agent
copy in site-packages can never satisfy the gate and no second copy of the
version constant exists anywhere.

Failure contract: every failure raises VersionGateError — a clear message on
stderr and exit 1, never a bare KeyError. Static-version projects keep the
original behavior: tag == [project].version, plus the dual_agent cross-check
when an attr mapping is wired in.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import re
import sys
from collections import namedtuple

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib is 3.11+ stdlib
    import tomli as tomllib

TAG_RE = re.compile(r"v\d+\.\d+\.\d+([-a-zA-Z0-9.]+)?")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+([-a-zA-Z0-9.]+)?")

GateResult = namedtuple("GateResult", "name version source")


class VersionGateError(Exception):
    """A version-gate failure with a human-readable reason (never a bare KeyError)."""


def load_pyproject(path):
    pyproject_path = pathlib.Path(path)
    if not pyproject_path.is_file():
        raise VersionGateError(f"pyproject.toml not found: {pyproject_path}")
    try:
        return tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise VersionGateError(
            f"pyproject.toml is not valid TOML: {pyproject_path}: {exc}") from exc


def declared_version(pyproject):
    """Return the static [project] version, or None when version is dynamic.

    Dynamic is the design of this repository: the declared version IS
    dual_agent.__version__ via the attr mapping, so None means "resolve the
    package truth" — never "read [project][\"version\"]".
    """
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise VersionGateError("pyproject.toml has no [project] table")
    if "version" in (project.get("dynamic") or []):
        return None
    if "version" in project:
        return project["version"]
    raise VersionGateError(
        'pyproject [project] declares neither a static "version" nor '
        '"version" in dynamic — cannot determine the distribution version')


def _dynamic_attr(pyproject):
    entry = (pyproject.get("tool", {}).get("setuptools", {})
             .get("dynamic", {}).get("version"))
    if isinstance(entry, dict):
        return entry.get("attr")
    return None


def _resolve_package_version(pyproject, root):
    """Import the version source inside THIS checkout and read the constant.

    The mapping in pyproject itself decides where the truth lives, so the
    gate never hard-codes a second copy of the path or the number.
    """
    attr = _dynamic_attr(pyproject)
    if not attr or not isinstance(attr, str):
        raise VersionGateError(
            "pyproject declares version dynamically but "
            "[tool.setuptools.dynamic] version.attr is missing — "
            "cannot resolve dual_agent.__version__")
    module_name, _, attr_name = attr.rpartition(".")
    if not module_name:
        raise VersionGateError(
            f"unsupported version.attr {attr!r}: expected '<package>.<attr>'")

    package_dir = pyproject.get("tool", {}).get("setuptools", {}).get(
        "package-dir", {})
    if module_name in package_dir:
        relative = package_dir[module_name]
    elif "" in package_dir:
        relative = f"{package_dir['']}/{module_name.replace('.', '/')}"
    else:
        relative = module_name.replace(".", "/")
    init_path = pathlib.Path(root) / relative / "__init__.py"
    if not init_path.is_file():
        raise VersionGateError(
            f"version source not found: {attr} resolves to {init_path}, "
            "which does not exist in this checkout")

    # Import by explicit file path so the gate always reads the checkout —
    # never an installed dual_agent that sys.path might surface first.
    spec = importlib.util.spec_from_file_location(module_name, init_path)
    if spec is None or spec.loader is None:
        raise VersionGateError(f"cannot import version source: {init_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise VersionGateError(
            f"importing version source {init_path} failed: {exc}") from exc
    version = getattr(module, attr_name, None)
    if not isinstance(version, str) or not version:
        raise VersionGateError(
            f"{init_path} does not define a string {attr_name} constant")
    return version


def check(tag=None, expected=None, pyproject_path="pyproject.toml"):
    """Run the gate. Returns GateResult(name, version, source) on success."""
    if (tag is None) == (expected is None):
        raise VersionGateError(
            "exactly one selector is required: --tag vX.Y.Z or --expected X.Y.Z")
    if tag is not None:
        if not TAG_RE.fullmatch(tag):
            raise VersionGateError(
                f"tag {tag!r} is not a strict vX.Y.Z[pre] tag; refusing to build")
        target = tag[1:]
        source = f"tag {tag}"
    else:
        if not VERSION_RE.fullmatch(expected):
            raise VersionGateError(
                f"expected version {expected!r} is not a strict X.Y.Z[pre] version")
        target = expected
        source = f"expected {expected}"

    pyproject = load_pyproject(pyproject_path)
    declared = declared_version(pyproject)
    name = pyproject.get("project", {}).get("name", "<unnamed>")
    root = pathlib.Path(pyproject_path).resolve().parent
    attr = _dynamic_attr(pyproject)

    problems = []
    if declared is not None and declared != target:
        problems.append(f"pyproject version={declared}")
    if declared is None or attr is not None:
        # dynamic single-truth, or a static project that also wires the
        # dual_agent cross-check — both must agree with the target
        pkg_version = _resolve_package_version(pyproject, root)
        if pkg_version != target:
            problems.append(f"{attr}={pkg_version}")

    if problems:
        raise VersionGateError(
            f"version mismatch: {source} (normalized {target}) vs "
            + ", ".join(problems))
    return GateResult(name=name, version=target, source=source)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Version consistency gate: tag/expected must equal "
                    "dual_agent.__version__ (pyproject reads it dynamically).")
    selectors = parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument("--tag", help="pushed release tag, e.g. v2.2.1")
    selectors.add_argument("--expected", help="expected bare version, e.g. 2.2.1")
    parser.add_argument("--pyproject", default="pyproject.toml",
                        help="path to pyproject.toml (default: ./pyproject.toml)")
    args = parser.parse_args(argv)
    try:
        result = check(tag=args.tag, expected=args.expected,
                       pyproject_path=args.pyproject)
    except VersionGateError as exc:
        print(f"version gate: FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"version gate OK: {result.name} {result.version} ({result.source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
