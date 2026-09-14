"""CU-TUI-5a (V3.2): first-run funnel — entry layer contract tests.

Mandate matrix (CU-TUI-5 v2.1 IMPLEMENTATION AUTHORIZATION):

- FIX-1 / P1-2: `_funnel_preflight` minimal intent classification BEFORE
  `_parse_cockpit_arguments`; every `--step` shape (including malformed
  `--step=`) never enters the funnel; unknown flags / dangling or invalid
  timeouts / a second task token all fall back to the original parser.
- FIX-3 / P1-1: `resolve_default_composition` is the single default
  binding source; `CompositionBinding` equality is four-field and
  includes `canonical_runtime_identity` (the existing evidence-path
  identity carried verbatim — never recomputed).
- Routing: the funnel is reachable ONLY via interactive TTY + importable
  cockpit_tui; pipe / --json / textual-missing keep the 2.5.0 byte
  contract exactly.
- Preview / Start consistency: start re-reads the live VERIFIED pool,
  resolves with the same pure function, and refuses to start on any
  composition change (COMPOSITION_CHANGED: zero slots / session /
  journal / boundary hook / driver).

All doubles are offline: scripted adapters, fake TUI modules, injected
evidence. No REAL providers, no real runtimes, no credentials, and the
real default evidence directory is never read.
"""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "dual-agent-development" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cockpit_entry
import host_entry
from candidate_validation import CandidateValidationStatus
from event_index import EventIndex
from external_runtime import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
)
from sequential_pipeline import RunStatus


# ---------------------------------------------------------------- doubles


class _OfflineAdapter:
    """Offline double mirroring test_cockpit_entry._ScriptedAdapter."""

    def __init__(self, runtime, provider):
        self.profile = SimpleNamespace(
            runtime=runtime, provider=provider, model=None,
            agent_id=f"{runtime}-agent")
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        count = len(self.requests)
        return InvocationResult(
            status=InvocationStatus.SUCCESS,
            output=f"output-{self.profile.runtime}-{count}",
            error=None,
            trace=InvocationTrace(
                invocation_id=f"inv-{self.profile.runtime}-{count}",
                task_id=request.task_id, agent_id=request.agent_id,
                runtime=self.profile.runtime,
                provider=self.profile.provider, model=None,
                role=request.role, status=InvocationStatus.SUCCESS))


def _factories(*adapters):
    return [lambda bound=adapter: bound for adapter in adapters]


def _descriptor_entry(runtime_id, provider_id, identity):
    """Duck-typed pool entry: exactly what resolve is allowed to read."""
    return SimpleNamespace(runtime_id=runtime_id, provider_id=provider_id,
                           identity=identity)


def _verified_evidence(registry, *runtime_ids):
    return {registry.get(rt).identity: SimpleNamespace(
        status=CandidateValidationStatus.VERIFIED)
        for rt in runtime_ids}


def _fake_funnel_tui(scripted_outcome="none"):
    """Offline cockpit_tui double with the CU-TUI-5 funnel surface.

    Records the hand-off kwargs; by default exercises nothing (returns
    None = the user quit before starting)."""
    module = ModuleType("cockpit_tui")
    module.calls = []
    module.textual_available = lambda: True
    module.new_event_store = EventIndex

    def run_funnel(**kwargs):
        module.calls.append(kwargs)
        return None

    module.run_cockpit_funnel = run_funnel
    module.run_cockpit_tui = lambda **kwargs: kwargs["driver"]()
    return module


# ------------------------------------------- FIX-1/P1-2: preflight grammar


class FunnelPreflightStepExclusionTests(unittest.TestCase):
    """P1-2 lock: every --step shape is a hard preflight exit."""

    STEP_VECTORS = (
        ["--step"],
        ["--step="],
        ["--step=x"],
        ["--step=architect=claude-cli"],
        ["--step=anything-invalid"],
        ["task", "--step"],
        ["task", "--step="],
        ["task", "--step=x"],
        ["--step", "anything"],
    )

    def test_step_shapes_never_enter_funnel(self):
        for argv in self.STEP_VECTORS:
            with self.subTest(argv=argv):
                self.assertIsNone(cockpit_entry._funnel_preflight(argv))

    def test_step_plus_json_stays_out(self):
        self.assertIsNone(
            cockpit_entry._funnel_preflight(["task", "--step=y", "--json"]))

    def test_step_hidden_in_timeout_value_slot_stays_out(self):
        # mirror of the original parser's pending-value swallow: the
        # value fails timeout coercion, so the shape degrades to the
        # original parser either way
        self.assertIsNone(
            cockpit_entry._funnel_preflight(["--timeout-seconds", "--step"]))

    def test_step_checks_precede_task_token_logic(self):
        source = cockpit_entry._funnel_preflight.__doc__ or ""
        self.assertIn("task token", source or "task token")
        # structural: both dedicated checks exist in the function body
        import inspect
        body = inspect.getsource(cockpit_entry._funnel_preflight)
        self.assertIn('== "--step"', body)
        self.assertIn('startswith("--step=")', body)
        first_task = body.find("task_token = token")
        self.assertGreater(first_task, 0)
        self.assertLess(body.find('== "--step"'), first_task)
        self.assertLess(body.find('startswith("--step=")'), first_task)


class FunnelPreflightGrammarTests(unittest.TestCase):
    """Minimal intent classification, nothing more."""

    def test_bare_argv_is_funnel_intent(self):
        intent = cockpit_entry._funnel_preflight([])
        self.assertIsNotNone(intent)
        self.assertIsNone(intent.task_token)
        self.assertIsNone(intent.timeout_seconds)

    def test_single_task_token_carried_verbatim(self):
        for token in ("some task", "", "   ", "task with spaces"):
            intent = cockpit_entry._funnel_preflight([token])
            self.assertEqual(intent.task_token, token)
            self.assertIsNone(intent.timeout_seconds)

    def test_json_never_funnel(self):
        self.assertIsNone(cockpit_entry._funnel_preflight(["--json"]))

    def test_unknown_flag_never_funnel(self):
        self.assertIsNone(
            cockpit_entry._funnel_preflight(["task", "--wat"]))

    def test_second_task_token_never_funnel(self):
        self.assertIsNone(cockpit_entry._funnel_preflight(["t1", "t2"]))

    def test_dangling_timeout_value_never_funnel(self):
        self.assertIsNone(
            cockpit_entry._funnel_preflight(["task", "--timeout-seconds"]))

    def test_invalid_timeout_never_funnel(self):
        self.assertIsNone(
            cockpit_entry._funnel_preflight(
                ["task", "--timeout-seconds", "abc"]))

    def test_inline_timeout_recognized(self):
        intent = cockpit_entry._funnel_preflight(["--timeout-seconds=60"])
        self.assertIsNone(intent.task_token)
        self.assertEqual(intent.timeout_seconds, 60)

    def test_detached_timeout_recognized_and_carried(self):
        intent = cockpit_entry._funnel_preflight(
            ["task text", "--timeout-seconds", "30"])
        self.assertEqual(intent.task_token, "task text")
        self.assertEqual(intent.timeout_seconds, 30)

    def test_multiple_timeouts_last_wins_like_original_parser(self):
        intent = cockpit_entry._funnel_preflight(
            ["--timeout-seconds", "60", "--timeout-seconds", "90"])
        self.assertEqual(intent.timeout_seconds, 90)

    def test_no_parser_error_surface_of_its_own(self):
        # preflight classifies; it never raises and never prints. Every
        # shape yields intent-or-None silently (bare argv is a valid
        # funnel intent whose composer starts empty).
        shapes = ([], ["task"], ["--step="], ["task", "--wat"],
                  ["task", "--timeout-seconds"], ["t1", "t2"],
                  ["task", "--timeout-seconds", "abc"])
        for argv in shapes:
            with self.subTest(argv=argv):
                intent = cockpit_entry._funnel_preflight(argv)
                self.assertTrue(
                    intent is None
                    or isinstance(intent, cockpit_entry.FunnelIntent))


# ------------------------------------- FIX-3/P1-1: resolve + four-field eq


class ResolveDefaultCompositionTests(unittest.TestCase):
    """The single default binding source: pure, deterministic, honest."""

    def _pool(self, *specs):
        # specs: (runtime_id, provider, fingerprint)
        return tuple(
            _descriptor_entry(rt, prov, (rt, prov, None, fingerprint))
            for rt, prov, fingerprint in specs)

    def test_pool_of_four_uses_full_template(self):
        composition = cockpit_entry.resolve_default_composition(
            self._pool(("rt-d", "prov-d", "default"),
                       ("rt-b", "prov-b", "default"),
                       ("rt-c", "prov-c", "default"),
                       ("rt-a", "prov-a", "default")))
        self.assertEqual(
            composition.roles, ("architect", "coder", "tester", "reviewer"))
        self.assertEqual(
            [b.runtime_id for b in composition.bindings],
            ["rt-a", "rt-b", "rt-c", "rt-d"])
        self.assertIsNone(composition.blocked_reason)

    def test_pool_of_three_drops_tester(self):
        composition = cockpit_entry.resolve_default_composition(
            self._pool(("rt-c", "p", "f"), ("rt-a", "p", "f"),
                       ("rt-b", "p", "f")))
        self.assertEqual(
            composition.roles, ("architect", "coder", "reviewer"))

    def test_pool_of_two_uses_pair(self):
        composition = cockpit_entry.resolve_default_composition(
            self._pool(("rt-b", "p", "f"), ("rt-a", "p", "f")))
        self.assertEqual(composition.roles, ("architect", "coder"))
        self.assertEqual(
            [b.runtime_id for b in composition.bindings], ["rt-a", "rt-b"])

    def test_pool_below_two_is_blocked_with_qualify_hint(self):
        for pool in ((), self._pool(("rt-a", "p", "f"))):
            composition = cockpit_entry.resolve_default_composition(pool)
            self.assertIsNotNone(composition.blocked_reason)
            self.assertIn("at least 2 VERIFIED", composition.blocked_reason)
            self.assertIn("found 0" if not pool else "found 1",
                          composition.blocked_reason)
            self.assertEqual(composition.blocked_hint,
                             host_entry._HINT_QUALIFY)
            self.assertEqual(composition.bindings, ())

    def test_input_ordering_is_normalized_to_sorted_runtime_ids(self):
        forward = cockpit_entry.resolve_default_composition(
            self._pool(("rt-a", "p", "f"), ("rt-b", "q", "f")))
        reverse = cockpit_entry.resolve_default_composition(
            self._pool(("rt-b", "q", "f"), ("rt-a", "p", "f")))
        self.assertEqual(forward, reverse)

    def test_binding_carries_role_runtime_provider_identity(self):
        pool = self._pool(("rt-a", "prov-a", "fp-1"),
                          ("rt-b", "prov-b", "fp-2"))
        composition = cockpit_entry.resolve_default_composition(pool)
        binding = composition.bindings[0]
        self.assertEqual(binding.role, "architect")
        self.assertEqual(binding.runtime_id, "rt-a")
        self.assertEqual(binding.provider_id, "prov-a")
        self.assertEqual(binding.canonical_runtime_identity,
                         ("rt-a", "prov-a", None, "fp-1"))

    def test_identity_is_carried_verbatim_not_recomputed(self):
        identity = ("rt-a", "prov-a", None, "fp-1")
        entry = _descriptor_entry("rt-a", "prov-a", identity)
        composition = cockpit_entry.resolve_default_composition(
            (entry, _descriptor_entry("rt-b", "prov-b",
                                      ("rt-b", "prov-b", None, "f"))))
        self.assertIs(composition.bindings[0].canonical_runtime_identity,
                      identity)

    def test_same_input_same_composition(self):
        pool = self._pool(("rt-b", "p", "f"), ("rt-a", "q", "f"))
        self.assertEqual(
            cockpit_entry.resolve_default_composition(pool),
            cockpit_entry.resolve_default_composition(pool))


class CompositionEqualityIdentityTests(unittest.TestCase):
    """P1-1: four-field equality — identity changes are always caught."""

    def _pool(self, *specs):
        return tuple(
            _descriptor_entry(rt, prov, (rt, prov, None, fingerprint))
            for rt, prov, fingerprint in specs)

    def test_identity_change_breaks_equality(self):
        pool_a = self._pool(("rt-a", "prov", "fp-A"),
                            ("rt-b", "prov", "fp"))
        pool_b = self._pool(("rt-a", "prov", "fp-B"),
                            ("rt-b", "prov", "fp"))
        composition_a = cockpit_entry.resolve_default_composition(pool_a)
        composition_b = cockpit_entry.resolve_default_composition(pool_b)
        self.assertNotEqual(composition_a, composition_b)

    def test_same_runtime_and_provider_cannot_mask_identity_change(self):
        binding_a = cockpit_entry.CompositionBinding(
            role="coder", runtime_id="rt-a", provider_id="prov",
            canonical_runtime_identity=("rt-a", "prov", None, "fp-A"))
        binding_b = cockpit_entry.CompositionBinding(
            role="coder", runtime_id="rt-a", provider_id="prov",
            canonical_runtime_identity=("rt-a", "prov", None, "fp-B"))
        self.assertNotEqual(binding_a, binding_b)

    def test_equal_identity_four_fields_equal(self):
        identity = ("rt-a", "prov", None, "fp-A")
        binding_a = cockpit_entry.CompositionBinding(
            role="coder", runtime_id="rt-a", provider_id="prov",
            canonical_runtime_identity=identity)
        binding_b = cockpit_entry.CompositionBinding(
            role="coder", runtime_id="rt-a", provider_id="prov",
            canonical_runtime_identity=tuple(identity))
        self.assertEqual(binding_a, binding_b)

    def test_runtime_id_string_alone_is_not_identity(self):
        # same runtime_id spelling, different canonical identity: the
        # pools differ, the compositions differ, and no string-only
        # shortcut may hide it
        single_a = self._pool(("rt-a", "prov", "fp-A"))
        single_b = self._pool(("rt-a", "prov", "fp-B"))
        resolve = cockpit_entry.resolve_default_composition
        # both blocked pools (size 1) but the blocked reason is the same
        # string — identity only matters through bindings, so verify at
        # binding level with a second runtime present
        pool_a = single_a + self._pool(("rt-b", "prov", "fp"))
        pool_b = single_b + self._pool(("rt-b", "prov", "fp"))
        self.assertNotEqual(resolve(pool_a), resolve(pool_b))


# ------------------------------------------------------- routing in main


class FunnelRoutingTests(unittest.TestCase):
    """cockpit_main: funnel only via interactive TTY + importable UI."""

    def setUp(self):
        self.adapters = (_OfflineAdapter("rt-a", "prov-a"),
                         _OfflineAdapter("rt-b", "prov-b"))
        self.factories = _factories(*self.adapters)

    def _run(self, argv, *, tui=None, terminal=False, evidence=None):
        out, err = io.StringIO(), io.StringIO()
        patches = []
        if tui is not None:
            patches.append(mock.patch.dict(sys.modules,
                                           {"cockpit_tui": tui}))
        if terminal:
            patches.append(mock.patch.object(cockpit_entry,
                                             "_terminal_present",
                                             return_value=True))
        with redirect_stdout(out), redirect_stderr(err):
            for patch in patches:
                patch.start()
            try:
                code = cockpit_entry.cockpit_main(
                    argv, factories=self.factories, evidence=evidence)
            finally:
                for patch in patches:
                    patch.stop()
        return code, out.getvalue(), err.getvalue()

    # -- funnel reached ------------------------------------------------

    def _offline_evidence(self):
        registry, _ = host_entry.environment_registry(self.factories)
        return _verified_evidence(registry, "rt-a", "rt-b")

    def test_bare_interactive_with_tui_enters_funnel(self):
        tui = _fake_funnel_tui()
        code, out, err = self._run([], tui=tui, terminal=True,
                                   evidence=self._offline_evidence())
        self.assertEqual(code, 0)          # pre-start quit: exit 0
        self.assertEqual(out, "")          # no delivery lines
        self.assertEqual(err, "")
        self.assertEqual(len(tui.calls), 1)
        kwargs = tui.calls[0]
        self.assertIsNone(kwargs["task_token"])
        self.assertIsNone(kwargs["timeout_seconds"])
        self.assertTrue(callable(kwargs["composition_preview"]))
        self.assertTrue(callable(kwargs["start_composition"]))

    def test_task_token_is_prefilled_into_funnel(self):
        tui = _fake_funnel_tui()
        self._run(["some task"], tui=tui, terminal=True,
                  evidence=self._offline_evidence())
        self.assertEqual(tui.calls[0]["task_token"], "some task")

    def test_timeout_carried_into_funnel(self):
        tui = _fake_funnel_tui()
        self._run(["task", "--timeout-seconds", "45"],
                  tui=tui, terminal=True, evidence=self._offline_evidence())
        self.assertEqual(tui.calls[0]["timeout_seconds"], 45)

    # -- funnel NOT reached: byte contract of the original parser ------

    def _assert_fail_bytes(self, argv, reason, detail, *, json_mode=False,
                           tui=None, terminal=False):
        code, out, err = self._run(argv, tui=tui, terminal=terminal)
        self.assertEqual(code, 2)
        stderr_line = f"dual-agent cockpit: {reason}: {detail}\n"
        if json_mode:
            expected = json.dumps(
                {"command": "cockpit", "status": "FAILED", "task_id": None,
                 "steps": [], "final_result": None,
                 "error": {"reason": reason, "detail": detail}},
                sort_keys=True, separators=(",", ":")) + "\n"
            self.assertEqual(out, expected)
        else:
            self.assertEqual(out, "")
        self.assertEqual(err, stderr_line)
        if tui is not None:
            self.assertEqual(tui.calls, [])
        return code, out, err

    def test_pipe_bare_keeps_invalid_task_bytes(self):
        self._assert_fail_bytes([], "INVALID_TASK", "task argument is missing")

    def test_pipe_task_without_step_keeps_missing_step_bytes(self):
        self._assert_fail_bytes(
            ["task"], "MISSING_STEP",
            "at least one --step ROLE=RUNTIME_ID is required")

    def test_json_bare_keeps_machine_line_bytes(self):
        self._assert_fail_bytes(
            ["--json"], "INVALID_TASK", "task argument is missing",
            json_mode=True)

    def test_step_shapes_stay_on_original_parser_even_interactive(self):
        # per-vector byte contract of the frozen parser (v2.1 九向量同源)
        tui = _fake_funnel_tui()
        vectors = (
            (["--step"], "MISSING_FLAG_VALUE", "--step requires a value"),
            (["--step="], "INVALID_TASK", "task argument is missing"),
            (["--step=x"], "INVALID_TASK", "task argument is missing"),
            (["task", "--step"], "MISSING_FLAG_VALUE",
             "--step requires a value"),
            (["task", "--step="], "INVALID_STEP_SPEC",
             "invalid --step value: "),
            (["task", "--step=x"], "INVALID_STEP_SPEC",
             "invalid --step value: x"),
        )
        for argv, reason, detail in vectors:
            with self.subTest(argv=argv):
                self._assert_fail_bytes(argv, reason, detail,
                                        tui=tui, terminal=True)

    def test_malformed_step_value_keeps_original_error(self):
        self._assert_fail_bytes(
            ["task", "--step="], "INVALID_STEP_SPEC", "invalid --step value: ")

    def test_unknown_flag_keeps_original_error_even_interactive(self):
        tui = _fake_funnel_tui()
        self._assert_fail_bytes(
            ["task", "--wat"], "UNSUPPORTED_ARGUMENT", "unknown flag: --wat",
            tui=tui, terminal=True)

    def test_invalid_timeout_keeps_original_error_even_interactive(self):
        # the original parser reaches timeout coercion only after the
        # steps parse cleanly, so the vector carries a valid --step
        tui = _fake_funnel_tui()
        self._assert_fail_bytes(
            ["task", "--step", "arch=rt-a", "--timeout-seconds", "abc"],
            "INVALID_TIMEOUT", "not a positive number: abc",
            tui=tui, terminal=True)

    def test_textual_missing_tty_keeps_original_bytes_without_hint(self):
        # v2.1: no extra hint line — byte-identical to 2.5.0
        with mock.patch.dict(sys.modules, {"cockpit_tui": None}):
            self._assert_fail_bytes(
                [], "INVALID_TASK", "task argument is missing", terminal=True)

    def test_developer_step_path_bypasses_funnel_entirely(self):
        tui = _fake_funnel_tui()
        code, out, err = self._run(
            ["task text", "--step", "arch=rt-a", "--step", "dev=rt-b"],
            tui=tui, terminal=True, evidence=self._offline_evidence())
        self.assertEqual(tui.calls, [])          # funnel never invoked
        self.assertEqual(code, 0)                # legacy TUI path ran


# --------------------------------------- preview / start consistency


class FunnelCompositionClosureTests(unittest.TestCase):
    """composition_preview + start_composition: single binding source,
    live re-read at start, honest refusal on any change."""

    def setUp(self):
        self.host = host_entry

    def _closures(self, evidence, *, factories=None, boundary_hook=None,
                  event_index=None):
        if factories is None:
            adapters = (_OfflineAdapter("rt-a", "prov-a"),
                        _OfflineAdapter("rt-b", "prov-b"))
            factories = _factories(*adapters)
        registry, skipped = host_entry.environment_registry(factories)
        surfaces = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence,
            timeout_seconds=None, boundary_hook=boundary_hook,
            observation_sink=None, event_index=event_index or EventIndex())
        return surfaces

    def _verified_evidence_two(self):
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        registry, _ = host_entry.environment_registry(_factories(*adapters))
        return _verified_evidence(registry, "rt-a", "rt-b")

    def test_preview_pool_empty_is_blocked(self):
        surfaces = self._closures({})
        preview = surfaces.preview()
        self.assertIsNotNone(preview.blocked_reason)
        self.assertIn("found 0", preview.blocked_reason)
        self.assertEqual(preview.blocked_hint, host_entry._HINT_QUALIFY)

    def test_preview_pool_of_two_binds_sorted(self):
        surfaces = self._closures(self._verified_evidence_two())
        preview = surfaces.preview()
        self.assertIsNone(preview.blocked_reason)
        self.assertEqual(
            [b.runtime_id for b in preview.bindings], ["rt-a", "rt-b"])

    def test_providerless_runtime_never_enters_pool(self):
        # a provider-less family is skipped at registration (identity
        # requires a non-empty provider); with only one VERIFIED runtime
        # left the pool is honestly blocked at found 1
        providerless = SimpleNamespace(
            profile=SimpleNamespace(runtime="rt-x", provider=None))
        working = _OfflineAdapter("rt-a", "prov-a")
        factories = [lambda: providerless, lambda bound=working: bound]
        registry, skipped = host_entry.environment_registry(factories)
        self.assertIn("rt-x", skipped)
        evidence = _verified_evidence(registry, "rt-a")
        surfaces = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        preview = surfaces.preview()
        self.assertIn("found 1", preview.blocked_reason)

    def test_start_blank_task_is_invalid(self):
        surfaces = self._closures(self._verified_evidence_two())
        result = surfaces.start("   ", surfaces.preview())
        self.assertEqual(result.reason, "INVALID_TASK")

    def test_start_blocked_pool_is_qualification_error(self):
        surfaces = self._closures({})
        result = surfaces.start("task", surfaces.preview())
        self.assertEqual(result.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("at least 2 VERIFIED", result.detail)
        self.assertEqual(result.hint, host_entry._HINT_QUALIFY)

    def test_start_happy_path_composes_execution(self):
        hooks = []
        surfaces = self._closures(
            self._verified_evidence_two(),
            boundary_hook=lambda boundary, execution_id:
                hooks.append((boundary, execution_id)))
        preview = surfaces.preview()
        composed = surfaces.start("funnel task", preview)
        self.assertTrue(callable(getattr(composed, "drive", None)))
        self.assertEqual(composed.task, "funnel task")
        self.assertEqual(composed.steps,
                         (("architect", "rt-a"), ("coder", "rt-b")))
        self.assertEqual(
            [line[0] for line in
             [(part[0],) for part in composed.plan]],
            ["step-0-architect", "step-1-coder"])
        outcome = composed.drive()
        self.assertIs(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(len(hooks), 1)     # assembled exactly once
        # control + revision surfaces are live
        receipt = composed.dispatch_control("PAUSE")
        self.assertIsNotNone(receipt)
        self.assertIsInstance(composed.revision_pending(), int)

    def test_start_refuses_on_identity_change_zero_start(self):
        # pool A: fingerprint fp-A; expected composition from A
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        factories = _factories(*adapters)
        registry_a, skipped = host_entry.environment_registry(factories)
        evidence_a = _verified_evidence(registry_a, "rt-a", "rt-b")
        hooks = []
        surfaces = cockpit_entry._funnel_composition_closures(
            registry_a, skipped, evidence_a, timeout_seconds=None,
            boundary_hook=lambda boundary, execution_id:
                hooks.append((boundary, execution_id)),
            observation_sink=None, event_index=EventIndex())
        preview = surfaces.preview()

        # pool B: same runtime ids/providers, rt-a fingerprint changed
        from runtime_adapter_registry import (  # noqa: E402 局部双件
            AdapterDescriptor, AdapterRegistry)
        registry_b = host_entry.environment_registry(factories)[0]
        # build a registry whose rt-a descriptor carries a different
        # config fingerprint by registering fresh descriptors
        fresh = AdapterRegistry()
        for descriptor in registry_b.list():
            fresh.register(AdapterDescriptor(
                runtime_id=descriptor.runtime_id,
                provider_id=descriptor.provider_id,
                runtime_type=descriptor.runtime_type,
                display_name=descriptor.display_name,
                adapter_factory=descriptor.adapter_factory,
                model_id=descriptor.model_id,
                config_fingerprint=(
                    "alt-fp" if descriptor.runtime_id == "rt-a"
                    else descriptor.config_fingerprint)))
        evidence_b = {fresh.get(rt).identity: SimpleNamespace(
            status=CandidateValidationStatus.VERIFIED)
            for rt in ("rt-a", "rt-b")}
        surfaces_b = cockpit_entry._funnel_composition_closures(
            fresh, (), evidence_b, timeout_seconds=None,
            boundary_hook=lambda boundary, execution_id:
                hooks.append((boundary, execution_id)),
            observation_sink=None, event_index=EventIndex())

        result = surfaces_b.start("task", preview)
        self.assertIsInstance(result, cockpit_entry.CompositionChanged)
        self.assertTrue(any("identity changed" in reason
                            for reason in result.reasons))
        self.assertEqual(hooks, [])         # zero assembly: no start
        # refreshed preview equals the changed composition; a retry with
        # the refreshed disclosure starts
        retry = surfaces_b.start("task", result.composition)
        self.assertTrue(callable(getattr(retry, "drive", None)))
        self.assertEqual(len(hooks), 1)

    def test_start_refuses_on_removed_runtime_with_honest_reason(self):
        # 3-runtime pool → one VERIFIED withdrawn: pool still ≥ 2, the
        # composition changes → CompositionChanged names the runtime
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"),
                    _OfflineAdapter("rt-c", "prov-c"))
        factories = _factories(*adapters)
        registry, skipped = host_entry.environment_registry(factories)
        evidence_full = _verified_evidence(registry, "rt-a", "rt-b", "rt-c")
        surfaces = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence_full, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        preview = surfaces.preview()          # 3-role template

        evidence_partial = {
            registry.get("rt-a").identity:
                SimpleNamespace(status=CandidateValidationStatus.VERIFIED),
            registry.get("rt-c").identity:
                SimpleNamespace(status=CandidateValidationStatus.VERIFIED)}
        surfaces_partial = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence_partial, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        result = surfaces_partial.start("task", preview)
        self.assertIsInstance(result, cockpit_entry.CompositionChanged)
        self.assertTrue(any("no longer VERIFIED" in reason
                            for reason in result.reasons))
        self.assertTrue(any("rt-b" in reason for reason in result.reasons))

    def test_start_on_pool_shrunk_below_two_is_blocked_error(self):
        # 2 → 1: the live pool is honestly blocked, and the blocked
        # branch precedes any changed-composition comparison (设计裁决:
        # live blocked → RUNTIME_NOT_QUALIFIED)
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        registry, skipped = host_entry.environment_registry(
            _factories(*adapters))
        evidence_full = _verified_evidence(registry, "rt-a", "rt-b")
        surfaces = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence_full, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        preview = surfaces.preview()
        evidence_one = {registry.get("rt-a").identity:
                        SimpleNamespace(
                            status=CandidateValidationStatus.VERIFIED)}
        surfaces_one = cockpit_entry._funnel_composition_closures(
            registry, skipped, evidence_one, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        result = surfaces_one.start("task", preview)
        self.assertIsInstance(result, cockpit_entry.CompositionError)
        self.assertEqual(result.reason, "RUNTIME_NOT_QUALIFIED")
        self.assertIn("found 1", result.detail)
        self.assertEqual(result.hint, host_entry._HINT_QUALIFY)

    def test_start_refuses_on_added_runtime_with_honest_reason(self):
        adapters = (_OfflineAdapter("rt-a", "prov-a"),)
        factories_a = _factories(*adapters)
        registry_a, skipped_a = host_entry.environment_registry(factories_a)
        adapters_b = adapters + (_OfflineAdapter("rt-b", "prov-b"),)
        registry_b, skipped_b = host_entry.environment_registry(
            _factories(*adapters_b))
        evidence_a = _verified_evidence(registry_a, "rt-a")
        evidence_b = _verified_evidence(registry_b, "rt-a", "rt-b")
        surfaces_a = cockpit_entry._funnel_composition_closures(
            registry_a, skipped_a, evidence_a, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        preview = surfaces_a.preview()      # blocked (found 1)
        surfaces_b = cockpit_entry._funnel_composition_closures(
            registry_b, skipped_b, evidence_b, timeout_seconds=None,
            boundary_hook=None, observation_sink=None,
            event_index=EventIndex())
        result = surfaces_b.start("task", preview)
        self.assertIsInstance(result, cockpit_entry.CompositionChanged)
        self.assertTrue(any("new VERIFIED runtime" in reason
                            for reason in result.reasons))

    def test_same_pool_two_starts_compose_independent_runs(self):
        evidence = self._verified_evidence_two()
        surfaces = self._closures(evidence)
        preview = surfaces.preview()
        first = surfaces.start("task one", preview)
        second = surfaces.start("task two", surfaces.preview())
        self.assertIsNot(first, second)
        self.assertEqual(first.drive().status, RunStatus.COMPLETED)
        self.assertEqual(second.drive().status, RunStatus.COMPLETED)


# ------------------------------------------------------- funnel delivery


class FunnelDeliveryTests(unittest.TestCase):
    """After a started funnel run: TERMINAL delivery mirrors the
    legacy human path; a pre-start quit delivers nothing."""

    def _funnel_tui_running(self):
        module = ModuleType("cockpit_tui")
        module.textual_available = lambda: True
        module.new_event_store = EventIndex
        module.run_cockpit_tui = lambda **kwargs: kwargs["driver"]()

        def run_funnel(**kwargs):
            preview = kwargs["composition_preview"]()
            composed = kwargs["start_composition"]("funnel task", preview)
            return composed.drive()

        module.run_cockpit_funnel = run_funnel
        return module

    def test_started_run_delivers_human_lines_and_exit_zero(self):
        adapters = (_OfflineAdapter("rt-a", "prov-a"),
                    _OfflineAdapter("rt-b", "prov-b"))
        factories = _factories(*adapters)
        registry, _ = host_entry.environment_registry(factories)
        evidence = _verified_evidence(registry, "rt-a", "rt-b")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules,
                             {"cockpit_tui": self._funnel_tui_running()}), \
                mock.patch.object(cockpit_entry, "_terminal_present",
                                  return_value=True), \
                redirect_stdout(out), redirect_stderr(err):
            code = cockpit_entry.cockpit_main([], factories=factories,
                                              evidence=evidence)
        self.assertEqual(code, 0)
        self.assertIn("Task: funnel task", out.getvalue())
        self.assertIn("Status: COMPLETED", out.getvalue())
        self.assertIn("architect / rt-a", out.getvalue())
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
