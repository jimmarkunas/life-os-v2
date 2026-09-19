import base64
import json
import threading
import time
from urllib.parse import unquote
from lifeos.core.http import HttpResponse

class FakeHttp:
    def __init__(self): self.calls=[]; self.created_processed=False
    def request_json(self, context, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        if method == "GET" and url.endswith("/labels"):
            labels=[{"id":"label-news","name":"J Newsletters"}]
            if self.created_processed: labels.append({"id":"label-processed","name":"J Newsletters/Processed"})
            return {"labels":labels}
        if method == "POST" and url.endswith("/labels"):
            self.created_processed=True; return {"id":"label-processed","name":"J Newsletters/Processed"}
        if method == "GET" and "/messages?" in url: return {"messages":[{"id":"msg-1"}]}
        if method == "GET" and "/messages/msg-1?format=full" in url:
            data=base64.urlsafe_b64encode(b"synthetic job alert").decode().rstrip("=")
            return {"id":"msg-1","internalDate":"1789574400000","payload":{"mimeType":"text/plain","headers":[{"name":"From","value":"alerts@example.invalid"},{"name":"Subject","value":"Synthetic"}],"body":{"data":data}}}
        if method == "POST" and url.endswith("/messages/msg-1/modify"): return {"id":"msg-1"}
        raise AssertionError((method,url,kwargs))

class BacklogFakeHttp(FakeHttp):
    def request_json(self, context, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        if method == "GET" and url.endswith("/labels"): return {"labels":[{"id":"label-news","name":"J Newsletters"},{"id":"label-processed","name":"J Newsletters/Processed"}]}
        if method == "GET" and "/messages?" in url: return {"messages":[{"id":"msg-new"}]} if "pageToken=page-2" in unquote(url) else {"messages":[{"id":"msg-old"},{"id":"msg-recent"}],"nextPageToken":"page-2"}
        for mid,date in {"msg-old":"1609459200000","msg-recent":"1789488000000","msg-new":"1789574400000"}.items():
            if method == "GET" and f"/messages/{mid}?format=full" in url:
                body=f"[Synthetic Labs\n90%\nProgram Manager\nRemote](https://jobright.ai/jobs/info/{mid})\nView More Opportunities"; data=base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
                return {"id":mid,"internalDate":date,"payload":{"mimeType":"text/plain","headers":[{"name":"From","value":"alerts@jobright.example.invalid"},{"name":"Subject","value":"Jobright jobs"}],"body":{"data":data}}}
        raise AssertionError((method,url,kwargs))

class DetailRetryFakeHttp(FakeHttp):
    def __init__(self, *, fail_message_id, permanent=False): super().__init__(); self.fail_message_id=fail_message_id; self.permanent=permanent; self.detail_attempts={}
    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and "/messages?" in url: return {"messages":[{"id":"msg-ok"},{"id":self.fail_message_id}]}
        for mid in ("msg-ok",self.fail_message_id):
            if method == "GET" and f"/messages/{mid}?format=full" in url:
                self.detail_attempts[mid]=self.detail_attempts.get(mid,0)+1
                if mid == self.fail_message_id and (self.permanent or self.detail_attempts[mid] == 1): raise TimeoutError(f"synthetic detail timeout for {mid}")
                data=base64.urlsafe_b64encode(f"body {mid}".encode()).decode().rstrip("=")
                return {"id":mid,"internalDate":"1789574400000","payload":{"mimeType":"text/plain","headers":[{"name":"From","value":"alerts@example.invalid"},{"name":"Subject","value":f"Synthetic {mid}"}],"body":{"data":data}}}
        return super().request_json(context,method,url,**kwargs)

class GoogleErrorBackend:
    def request(self, method, url, *, headers, body, timeout_seconds):
        if method == "GET" and url.endswith("/labels"): return HttpResponse(200,{},json.dumps({"labels":[{"id":"label-news","name":"J Newsletters"},{"id":"label-processed","name":"J Newsletters/Processed"}]}).encode())
        if method == "GET" and "/messages?" in url: return HttpResponse(200,{},json.dumps({"messages":[{"id":"msg-stuck"}]}).encode())
        if method == "GET" and "/messages/msg-stuck?format=full" in url: return HttpResponse(403,{},json.dumps({"error":{"code":403,"message":"User rate limit exceeded for https://gmail.googleapis.com Authorization: Bearer synthetic-secret-token","errors":[{"domain":"usageLimits","reason":"rateLimitExceeded","message":"User rate limit exceeded"}],"status":"PERMISSION_DENIED"}}).encode())
        raise AssertionError((method,url))

class AdmissionFakeHttp:
    def __init__(self): self.detail_ids=[]; self.processed=set()
    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and url.endswith("/labels"): return {"labels":[{"id":"label-news","name":"J Newsletters"},{"id":"label-processed","name":"J Newsletters/Processed"}]}
        if method == "GET" and "/messages?" in url:
            remaining=[x for x in ("E","D","C","B","A") if x not in self.processed]; return {"messages":[{"id":x} for x in remaining]}
        if method == "GET" and "?format=full" in url:
            mid=url.split("/messages/",1)[1].split("?",1)[0]; self.detail_ids.append(mid); data=base64.urlsafe_b64encode(f"body {mid}".encode()).decode().rstrip("=")
            return {"id":mid,"internalDate":"1700000000000","payload":{"mimeType":"text/plain","headers":[{"name":"From","value":"alerts@example.invalid"},{"name":"Subject","value":mid}],"body":{"data":data}}}
        if method == "POST" and url.endswith("/modify"):
            mid=url.split("/messages/",1)[1].split("/modify",1)[0]; self.processed.add(mid); return {"id":mid}
        raise AssertionError((method,url,kwargs))

class SerialDetailFakeHttp(AdmissionFakeHttp):
    def __init__(self): super().__init__(); self.active=0; self.max_active=0; self.gaps=[]
    def request_json(self, context, method, url, **kwargs):
        if method == "GET" and "?format=full" in url:
            self.active += 1; self.max_active=max(self.max_active,self.active); started=time.monotonic()
            try: return super().request_json(context,method,url,**kwargs)
            finally: self.active -= 1; self.gaps.append(time.monotonic()-started)
        return super().request_json(context,method,url,**kwargs)
