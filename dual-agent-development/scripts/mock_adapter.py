from __future__ import annotations
from dual_agent import *

class MockAdapter:
    def __init__(self, adapter_id="mock", profile=None, discovery_status=DiscoveryStatus.AVAILABLE, response=None, exception=None):
        self.adapter_id=adapter_id; self._profile=profile or AdapterProfile(adapter_id,frozenset({"edit"}),frozenset({"code"}),True,90,(Evidence("default","edit",EvidenceStatus.VERIFIED),)); self.discovery_status=discovery_status; self.response=response; self.exception=exception; self._canceled=set(); self.invocations={}
    def discover(self):
        if self.discovery_status is not DiscoveryStatus.AVAILABLE: return self.discovery_status
        return self._profile
    def invoke(self, request):
        if request.handle and request.handle.token in self._canceled: return {"status":"CANCELED"}
        if request.handle: self.invocations[request.handle.token]=request
        if self.exception: raise self.exception
        if self.response is None: return {"status":"SUCCESS","output":request.task.payload}
        return self.response
    def normalize(self, raw):
        if not isinstance(raw,dict) or not isinstance(raw.get("status"),str): return Result(InvokeStatus.MALFORMED,error="malformed")
        try: status=InvokeStatus(raw["status"])
        except ValueError: return Result(InvokeStatus.MALFORMED,error="unknown status")
        return Result(status,raw.get("output"),raw.get("error"))
    def cancel(self, handle):
        if not isinstance(handle,Handle) or handle.adapter_id != self.adapter_id: return Result(InvokeStatus.FAILED,error="invalid handle")
        self._canceled.add(handle.token); return Result(InvokeStatus.CANCELED)
