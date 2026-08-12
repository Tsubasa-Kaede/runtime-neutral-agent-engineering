"""Small provider-neutral offline routing and review core."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping, Protocol, Sequence
from uuid import uuid4

class EvidenceStatus(str, Enum): VERIFIED="VERIFIED"; UNKNOWN="UNKNOWN"; UNVERIFIED="UNVERIFIED"
class Metric(str, Enum): RELIABILITY="reliability"; CAPABILITY="capability"
class TaskKind(str, Enum): CODE="code"; REVIEW="review"
class DiscoveryStatus(str, Enum): AVAILABLE="AVAILABLE"; UNAVAILABLE="UNAVAILABLE"; FAILED="FAILED"
class InvokeStatus(str, Enum): SUCCESS="SUCCESS"; MALFORMED="INVALID_OUTPUT"; UNAVAILABLE="UNAVAILABLE"; FAILED="FAILED"; CANCELED="CANCELED"

@dataclass(frozen=True)
class Evidence: ref: str; claim: str; status: EvidenceStatus
@dataclass(frozen=True)
class AdapterProfile:
    adapter_id: str; capabilities: FrozenSet[str]; task_kinds: FrozenSet[str]; supports_cancel: bool; reliability: int; evidence: tuple[Evidence,...]=()
    def __post_init__(self):
        if (not isinstance(self.adapter_id, str) or not self.adapter_id or not isinstance(self.reliability, int) or isinstance(self.reliability, bool) or not 0 <= self.reliability <= 100 or not all(isinstance(x,str) and x for x in self.capabilities|self.task_kinds) or not isinstance(self.supports_cancel,bool) or not all(isinstance(e,Evidence) for e in self.evidence)): raise ValueError("invalid profile")
@dataclass(frozen=True)
class Task: kind: str; role: str|None=None; payload: Any=None
@dataclass(frozen=True)
class RoutePolicy:
    required_capabilities: FrozenSet[str]; weights: Mapping[str,int]; min_score: int; require_cancellation: bool=False
    def __post_init__(self):
        if not isinstance(self.min_score, int) or isinstance(self.min_score, bool) or not 0 <= self.min_score <= 100 or any(not isinstance(v,int) or isinstance(v,bool) or v <= 0 for v in self.weights.values()): raise ValueError("invalid policy")
@dataclass(frozen=True)
class RolePolicy:
    mapping: Mapping[tuple[str,str],str]
    def resolve(self, role: str|None, kind: str) -> str: return self.mapping.get((role or "",kind), kind)
@dataclass(frozen=True)
class Candidate:
    adapter_id: str; score: int|None; gate_failures: tuple[str,...]; evidence_refs: tuple[str,...]
@dataclass(frozen=True)
class RouteResult:
    adapter_id: str|None; reason: str; candidates: tuple[Candidate,...]=()

class Adapter(Protocol):
    def discover(self) -> AdapterProfile|DiscoveryStatus: ...
    def invoke(self, request: InvokeRequest) -> Any: ...
    def cancel(self, handle: Handle) -> Any: ...
    def normalize(self, raw: Any) -> Result: ...
@dataclass(frozen=True)
class InvokeRequest: task: Task; handle: Handle|None=None
@dataclass(frozen=True)
class Handle: adapter_id: str; token: str
@dataclass(frozen=True)
class Result: status: InvokeStatus; output: Any=None; error: str|None=None

class Router:
    def __init__(self, profiles: Sequence[AdapterProfile], policies: Mapping[str,RoutePolicy], adapters: Mapping[str,Adapter]|None=None): self.profiles=tuple(profiles); self.policies=dict(policies); self.adapters=dict(adapters or {}); self._active={}; self._canceled=set()
    def prepare_invoke(self, adapter_id, request):
        if adapter_id not in self.adapters or not isinstance(request, InvokeRequest): return None
        handle=Handle(adapter_id, uuid4().hex); self._active[(adapter_id,handle.token)]=handle
        return handle
    def run_invoke(self, handle, request):
        if not isinstance(handle,Handle): return Result(InvokeStatus.FAILED,error="invalid handle")
        key=(handle.adapter_id,handle.token)
        if key not in self._active: return Result(InvokeStatus.CANCELED,error="inactive handle")
        try:
            if not isinstance(request,InvokeRequest): return Result(InvokeStatus.FAILED,error="invalid request")
            adapter=self.adapters[handle.adapter_id]
            raw=adapter.invoke(InvokeRequest(request.task,handle))
            if key in self._canceled or key not in self._active: return Result(InvokeStatus.CANCELED)
            try: result=adapter.normalize(raw)
            except Exception as exc: return Result(InvokeStatus.MALFORMED,error=str(exc))
            if not isinstance(result,Result): return Result(InvokeStatus.MALFORMED,error="invalid output")
            return result
        except Exception as exc: return Result(InvokeStatus.FAILED,error=str(exc))
        finally: self._active.pop(key,None)
    def invoke(self, adapter_id, request):
        handle=self.prepare_invoke(adapter_id,request)
        if handle is None: return Result(InvokeStatus.UNAVAILABLE,error="adapter unavailable")
        return (handle,self.run_invoke(handle,request))
    def normalize(self, adapter_id, raw):
        adapter=self.adapters.get(adapter_id)
        if adapter is None: return Result(InvokeStatus.UNAVAILABLE,error="adapter unavailable")
        try: return adapter.normalize(raw)
        except Exception as exc: return Result(InvokeStatus.MALFORMED,error=str(exc))
    def cancel(self, adapter_id, handle):
        adapter=self.adapters.get(adapter_id)
        if adapter is None: return Result(InvokeStatus.UNAVAILABLE,error="adapter unavailable")
        if not isinstance(handle, Handle) or handle.adapter_id != adapter_id or (adapter_id, handle.token) not in self._active: return Result(InvokeStatus.FAILED,error="unknown handle")
        try:
            result=adapter.cancel(handle)
            if not isinstance(result,Result): return Result(InvokeStatus.MALFORMED,error="invalid cancel result")
            self._canceled.add((adapter_id,handle.token))
            return result
        except Exception as exc: return Result(InvokeStatus.FAILED,error=str(exc))
        finally: self._active.pop((adapter_id,handle.token),None)
    @classmethod
    def from_adapters(cls, adapters: Sequence[Adapter], policies: Mapping[str,RoutePolicy]) -> Router:
        profiles=[]
        instances={}
        for adapter in adapters:
            try:
                value=adapter.discover()
                if isinstance(value, AdapterProfile): profiles.append(value); instances[value.adapter_id]=adapter
            except Exception: pass
        return cls(profiles, policies, instances)
    def route(self, task: Task|None, role_policy: RolePolicy|None=None) -> RouteResult:
        if not isinstance(task,Task) or not isinstance(task.kind,str): return RouteResult(None,"INVALID_INPUT")
        kind=role_policy.resolve(task.role,task.kind) if role_policy else task.kind
        policy=self.policies.get(kind)
        if policy is None: return RouteResult(None,"NO_ROUTE")
        candidates=[]
        for p in sorted(self.profiles,key=lambda x: x.adapter_id if isinstance(x.adapter_id,str) else ""):
            failures=[]; refs=tuple(e.ref for e in p.evidence if e.status is EvidenceStatus.VERIFIED)
            if not isinstance(p.adapter_id,str): failures.append("invalid_adapter_id")
            if kind not in p.task_kinds: failures.append("task_kind")
            if not policy.required_capabilities.issubset(p.capabilities): failures.append("capability")
            verified_claims={e.claim for e in p.evidence if e.status is EvidenceStatus.VERIFIED}
            if any(c not in verified_claims for c in policy.required_capabilities): failures.append("evidence")
            if policy.require_cancellation and not p.supports_cancel: failures.append("cancellation")
            score=None
            if not failures:
                metrics={"reliability":p.reliability,"capability":100*len(policy.required_capabilities & p.capabilities)//max(1,len(policy.required_capabilities))}
                total=sum(w for w in policy.weights.values() if isinstance(w,int) and w>0)
                score=sum(policy.weights[k]*metrics.get(k,0) for k in policy.weights if isinstance(policy.weights[k],int) and policy.weights[k]>0)//total if total else 0
                if score < policy.min_score: failures.append("min_score")
            candidates.append(Candidate(p.adapter_id,score,tuple(failures),refs))
        eligible=[(c,next(p for p in self.profiles if p.adapter_id==c.adapter_id)) for c in candidates if not c.gate_failures and c.score is not None]
        if not eligible: return RouteResult(None,"NO_ROUTE",tuple(candidates))
        eligible.sort(key=lambda cp:(-cp[0].score,-cp[1].reliability,cp[0].adapter_id))
        return RouteResult(eligible[0][0].adapter_id,"ROUTED",tuple(candidates))

class ReviewState(str,Enum): OPEN="OPEN"; NEED_FIX="NEED_FIX"; PASS="PASS"; BLOCKED="BLOCKED"; ARCHITECTURE_VIOLATION="ARCHITECTURE_VIOLATION"
@dataclass(frozen=True)
class Finding: finding_id:str; message:str; signature:str; status:str="OPEN"; closed_by:str|None=None
@dataclass(frozen=True)
class ReviewSnapshot: state:ReviewState; round:int; findings:tuple[Finding,...]
class ReviewStateMachine:
    def __init__(self): self._snapshot=ReviewSnapshot(ReviewState.OPEN,0,()); self._terminal=False; self._history=set()
    @property
    def snapshot(self): return self._snapshot
    def apply(self,event:str,findings:Sequence[Finding]=(),evidence:Sequence[Evidence]=()) -> ReviewSnapshot:
        if self._terminal: return self._snapshot
        fs=tuple(findings)
        previous={f.finding_id:f for f in self._snapshot.findings}
        if any(f.finding_id in previous and previous[f.finding_id].signature != f.signature for f in fs if isinstance(f,Finding)): return self._block(self._snapshot.findings)
        if event in {"OPEN","PASS"} and not evidence: return self._block(self._snapshot.findings)
        if any(not isinstance(e,Evidence) or e.status is not EvidenceStatus.VERIFIED for e in evidence): return self._block(self._snapshot.findings)
        if any(not isinstance(f,Finding) or not isinstance(f.finding_id,str) or not f.finding_id.strip() or not isinstance(f.message,str) or not isinstance(f.signature,str) or not f.signature.strip() or f.status not in {"OPEN","RESOLVED"} or (f.closed_by is not None and not isinstance(f.closed_by,str)) or (f.status=="OPEN" and f.closed_by is not None) for f in fs): return self._block(self._snapshot.findings)
        if len({f.finding_id for f in fs}) != len(fs) or len({f.signature for f in fs}) != len(fs): return self._block(self._snapshot.findings)
        merged=dict(previous); merged.update({f.finding_id:f for f in fs}); active=tuple(merged.values())
        if len({f.finding_id for f in active}) != len(active) or len({f.signature for f in active}) != len(active): return self._block(self._snapshot.findings)
        unresolved={f.signature for f in fs if f.status != "RESOLVED"}
        if unresolved & self._history and event != "NEED_FIX": return self._block(self._snapshot.findings)
        if any(f.status=="RESOLVED" and (f.closed_by or "").strip().lower()!="controller" for f in fs): return self._block(self._snapshot.findings)
        if event not in {"OPEN","NEED_FIX","PASS","ARCHITECTURE_VIOLATION"}: return self._block(self._snapshot.findings)
        new_history=self._history | unresolved
        if event=="ARCHITECTURE_VIOLATION": return self._finish(ReviewState.ARCHITECTURE_VIOLATION,active)
        self._history=new_history
        if event=="NEED_FIX":
            self._snapshot=ReviewSnapshot(ReviewState.NEED_FIX,self._snapshot.round+1,active)
            if self._snapshot.round>=3: return self._finish(ReviewState.BLOCKED,active)
            return self._snapshot
        if event=="PASS" and (any(f.status=="OPEN" for f in active) or not evidence): return self._block(self._snapshot.findings)
        if event=="PASS": return self._finish(ReviewState.PASS,active)
        self._snapshot=ReviewSnapshot(ReviewState.OPEN,self._snapshot.round,active)
        return self._snapshot
    def _block(self,fs): return self._finish(ReviewState.BLOCKED,fs)
    def _finish(self,state,fs): self._snapshot=ReviewSnapshot(state,self._snapshot.round,fs); self._terminal=True; return self._snapshot
