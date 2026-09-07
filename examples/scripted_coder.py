"""A minimal custom adapter module — the offline stand-in for a real runtime.

Purpose
    This is the smallest honest example of a *custom adapter module* for
    Remote Collaboration: a module that exposes ``build(profile=None)`` and
    returns an adapter whose ``invoke`` answers with one valid
    ImplementationPacket JSON payload. The remote process imports THIS
    module (declared by name plus its directory as ``source_path``) and
    constructs the adapter inside its own process.

    The adapter is scripted — it never calls a model, never touches the
    network, and never reads credentials. Every result it produces is
    honestly labeled offline.

Prerequisite
    None. Python only, from a source checkout of this repository.

How to use
    You normally do not run this file directly. See
    ``examples/remote_offline_demo.py``, which declares it:

        importable_adapter_factory(
            "scripted_coder", "build",
            profile={...},                       # must match the binding
            source_path=str(Path(__file__).parent),  # where this file lives
        )

    To write your own adapter module, copy this file and replace the
    canned response inside ``invoke`` with your runtime's real call —
    keep the ``build(profile=None)`` signature and return either an
    adapter or ``None`` (an honest construction failure).
"""
from external_runtime import (
    InvocationResult,
    InvocationStatus,
    InvocationTrace,
    new_invocation_id,
)

# One valid ImplementationPacket payload. The remote endpoint parses this
# through the production packet contract (fence strip -> JSON parse ->
# task-id authority -> list normalization -> from_dict -> safety scan),
# so it must satisfy exactly that schema.
OUTPUT_TEXT = """{
  "task_id": "scripted-task",
  "role": "coder",
  "changed_files": [],
  "implementation_summary": "Scripted implementation summary (offline demonstration).",
  "implementation_details": ["single canned response, no runtime invoked"],
  "assumptions": [],
  "unresolved_items": [],
  "test_requirements": ["the offline demo completes one round trip"]
}"""


class ScriptedAdapter:
    """A fixed-response adapter: same contract as a real adapter, no model."""

    def __init__(self, profile=None):
        self.profile = profile

    def invoke(self, request):
        trace = InvocationTrace(
            invocation_id=new_invocation_id(),
            task_id=request.task_id,
            agent_id=request.agent_id,
            runtime="scripted-runtime",
            provider=None,
            model=None,
            role=request.role,
            status=InvocationStatus.SUCCESS,
            duration_ms=1,
            exit_code=0,
        )
        return InvocationResult(
            status=InvocationStatus.SUCCESS,
            output=OUTPUT_TEXT,
            trace=trace,
        )


def build(profile=None):
    """Factory convention: single ``profile`` keyword, adapter or None."""
    return ScriptedAdapter(profile=profile)
