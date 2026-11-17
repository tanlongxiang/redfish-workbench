"""Strict task-scoped Redfish session. No Basic fallback, redirects or disk credentials."""
from __future__ import annotations
import gzip,http.client,io,json,re,socket,ssl,urllib.error,urllib.request,urllib.parse
from bmc_client import BmcClient,BmcError,normalize_target,NoRedirect

SESSION='/redfish/v1/SessionService/Sessions'
MAX_BODY=32*1024*1024
class RedfishHTTPError(BmcError):
    def __init__(self,status):
        self.status=status
        super().__init__(f'Redfish HTTP {status}；未自动重试或重新认证')

class ManagedSession:
    def __init__(self,address,timeout=20,insecure=False):
        self.base=normalize_target(address);self.timeout=timeout;self.session_uri=None;self.token=None
        self.cleanup='not_created';self.session_timeout=None;self.secrets=[];self.closed=False
        context=ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),NoRedirect())
    def path(self,value):
        # Reuse reviewed same-origin enforcement, without creating another transport.
        path=BmcClient.safe_path(self,value)
        raw=urllib.parse.urlsplit(path).path
        if '%' in raw or '\\' in raw or '//' in raw or any(v in {'.','..'} for v in raw.split('/')):raise BmcError('资源路径含未适配的编码或路径跳转')
        return path
    def safe(self,value):
        if isinstance(value,dict):return {k:('[REDACTED]' if re.search(r'(?i)(password|token|cookie|authorization|authkey|secret)',str(k)) else self.safe(v)) for k,v in value.items()}
        if isinstance(value,list):return [self.safe(v) for v in value]
        if isinstance(value,str):
            for secret in sorted(set(self.secrets),key=len,reverse=True):
                if secret:value=value.replace(secret,'[REDACTED]')
            return re.sub(r'(?i)(https?|s?ftp|scp)://[^/\s@]+@',r'\1://[REDACTED]@',value)
        return value
    def _remember_headers(self,headers):
        values={k.lower():v for k,v in headers.items()}
        token=values.get('x-auth-token')
        if token and re.fullmatch(r'[\x21-\x7e]{1,4096}',token):
            self.token=token;self.secrets.append(token)
        if values.get('location'):
            candidate=self.path(values['location'])
            if re.fullmatch(re.escape(SESSION)+r'/[A-Za-z0-9_-]+',candidate):self.session_uri=candidate
    def _http(self,method,path,payload=None,*,headers=None,binary=False):
        path=self.path(path)
        request_headers={'Accept':'application/json','OData-Version':'4.0'}
        if self.token:request_headers['X-Auth-Token']=self.token
        request_headers.update(headers or {})
        if payload is not None and not isinstance(payload,bytes):
            payload=json.dumps(payload,allow_nan=False).encode();request_headers['Content-Type']='application/json'
        request=urllib.request.Request(self.base+path,data=payload,headers=request_headers,method=method)
        try:
            with self.opener.open(request,timeout=self.timeout) as response:
                if method=='POST' and path==SESSION:self._remember_headers(response.headers)
                body=response.read(MAX_BODY+1)
                if len(body)>MAX_BODY:raise BmcError('响应超过32MiB，已停止读取')
                if body.startswith(b'\x1f\x8b'):
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as z:body=z.read(MAX_BODY+1)
                    if len(body)>MAX_BODY:raise BmcError('解压响应超过32MiB')
                if not body.strip():data={}
                else:
                    try:data=json.loads(body)
                    except (ValueError,UnicodeDecodeError):
                        if not binary:raise BmcError('设备返回非JSON响应；未执行后续步骤') from None
                        data=body
                if isinstance(data,dict) and 'error' in data:raise BmcError('设备返回Redfish错误；未执行后续步骤')
                return data,dict(response.headers),response.status
        except urllib.error.HTTPError as exc:
            status=exc.code;exc.close();raise RedfishHTTPError(status) from None
        except (urllib.error.URLError,TimeoutError,socket.timeout,ssl.SSLError,http.client.HTTPException,OSError):
            raise BmcError('Redfish连接失败或超时；请求结果可能未知，未自动重试') from None
    def get(self,path):return self._http('GET',path)
    def login(self,username,password):
        if self.token or self.closed:raise BmcError('会话客户端不可重复登录')
        if not username or not password:raise BmcError('需要本次账号密码')
        self.secrets.append(password)
        self.cleanup='unconfirmed'
        try:
            data,headers,status=self._http('POST',SESSION,{'UserName':username,'Password':password})
            headers={k.lower():v for k,v in headers.items()}
            token=headers.get('x-auth-token')
            if token and re.fullmatch(r'[\x21-\x7e]{1,4096}',token):self.token=token;self.secrets.append(token)
            location=headers.get('location') or (data.get('@odata.id') if isinstance(data,dict) else None)
            if location:
                candidate=self.path(location)
                if re.fullmatch(re.escape(SESSION)+r'/[A-Za-z0-9_-]+',candidate):self.session_uri=candidate
            self.cleanup='unconfirmed'
            if status!=201 or not self.token or not self.session_uri:
                raise BmcError('未取得201响应、Token及本次Session URI；拒绝开始任务，会话清理状态需核对')
            try:
                service,_,_=self.get('/redfish/v1/SessionService')
                timeout=service.get('SessionTimeout') if isinstance(service,dict) else None
                if type(timeout) is int and timeout>0:self.session_timeout=timeout
            except BmcError:pass  # Timeout unknown does not mean infinite or zero lifetime.
            return self
        except Exception:
            self.close();raise
    def close(self):
        if self.closed:return self.cleanup
        try:
            if self.session_uri and self.token:
                self.timeout=min(self.timeout,5)
                try:
                    _,_,status=self._http('DELETE',self.session_uri)
                    self.cleanup='deleted' if status in (200,204) else 'unconfirmed'
                except RedfishHTTPError as exc:
                    self.cleanup='already_absent' if exc.status in (404,410) else 'unconfirmed'
                except BmcError:self.cleanup='unconfirmed'
        finally:
            self.token=None;self.session_uri=None;self.secrets.clear();self.opener=None;self.closed=True
        return self.cleanup
