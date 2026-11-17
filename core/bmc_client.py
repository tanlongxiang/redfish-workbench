"""Reviewed standard-library transport derived from BMC_SN_Tool_v1.8.0.
Only GET and this client's own Redfish login/logout are allowed.
"""
from __future__ import annotations
import base64, gzip, http.client, http.cookiejar, io, ipaddress, json, re, socket, ssl
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
VERSION = 'node-verify-0.1.0'
SESSION_PATH = '/redfish/v1/SessionService/Sessions'

class BmcError(Exception):
    pass

class CertificateError(BmcError):
    pass

class ApiError(BmcError):
    def __init__(self, status, path):
        self.status = status
        self.path = path
        messages = {401: '认证失败或会话已失效，请检查账号密码', 403: '账号无此接口的读取权限，或设备要求额外认证', 404: '设备没有此接口（固件可能不同）', 405: '设备不支持此认证方式，请手动选择另一认证方式', 429: '设备限制访问频率，请稍后再试', 503: 'BMC 服务繁忙，请稍后再试'}
        super().__init__(f'{messages.get(status, "设备返回错误")} [HTTP {status}, {path}]')

def normalize_target(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise BmcError('BMC IP 为空')
    if '://' not in value:
        # A bare IPv6 address must be bracketed before adding a scheme.
        try:
            direct_ip = ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            value = f'[{direct_ip}]' if direct_ip.version == 6 else str(direct_ip)
        value = 'https://' + value
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme.lower() != 'https':
            raise BmcError('只支持 HTTPS，请填写 IP 或 https://IP')
        if parsed.username is not None or parsed.password is not None:
            raise BmcError('不要将账号密码写进 URL，请使用账号密码输入框')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise BmcError('BMC 地址仅填写 IP 和端口，不包含接口路径、查询或片段')
        address = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError:
        raise BmcError('BMC 地址格式无效，请填写 IPv4 / IPv6 地址，可附 HTTPS 端口') from None
    host = f'[{address}]' if address.version == 6 else str(address)
    return f'https://{host}' + (f':{port}' if port and port != 443 else '')

def stamp() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')

def serial_text(value) -> str:
    if value is None or isinstance(value, (bool, float, dict, list)):
        return ''
    text = str(value).strip()
    return '' if text.lower() in ('', '-', 'n/a', 'na', 'null', 'none', 'unknown') else text

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials, cookies or tokens to a redirect destination.
        raise BmcError('设备返回重定向；已停止，请使用实际 BMC HTTPS IP 和端口')

class BmcClient:
    def __init__(self, target, timeout=15, allow_self_signed=False):
        self.base = normalize_target(target)
        self.timeout = timeout
        self.headers = {'Accept': 'application/json', 'Accept-Encoding': 'gzip', 'User-Agent': 'BMC-SN-Reader/' + VERSION, 'OData-Version': '4.0'}
        self.jar = http.cookiejar.CookieJar()
        context = ssl._create_unverified_context() if allow_self_signed else ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar), urllib.request.HTTPSHandler(context=context), NoRedirect())
        self.session_uri = None
        self.credential_verified = False
        self.root = {}
        self.phase = '准备连接'

    def safe_path(self, path):
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(self.base + '/', path))
        try:
            origin = normalize_target(f'{parsed.scheme}://{parsed.netloc}')
        except BmcError:
            raise BmcError('设备返回的链接地址无效') from None
        if origin != self.base or parsed.username is not None or parsed.password is not None:
            raise BmcError('设备返回跨地址链接，已拒绝发送认证信息')
        return urllib.parse.urlunsplit(('', '', parsed.path, parsed.query, ''))

    def request(self, method, path, payload=None):
        path = self.safe_path(path)
        self.phase = f'{method} {path}'
        if method == 'POST' and path != SESSION_PATH:
            raise BmcError('工具仅允许创建本次登录会话')
        if method == 'DELETE' and (path != self.session_uri or not re.fullmatch(re.escape(SESSION_PATH) + r'/[^/?]+', path)):
            raise BmcError('工具仅允许注销本次创建的会话')
        if method not in ('GET', 'POST', 'DELETE'):
            raise BmcError('不允许此操作')
        headers = dict(self.headers)
        for cookie in self.jar:
            if cookie.name == 'XSRF-TOKEN':
                headers['X-XSRF-TOKEN'] = urllib.parse.unquote(cookie.value)
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if raw is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.base + path, data=raw, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise BmcError('接口响应超过 8 MB，已停止解析')
                if body.startswith(b'\x1f\x8b'):
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        body = compressed.read(8 * 1024 * 1024 + 1)
                    if len(body) > 8 * 1024 * 1024:
                        raise BmcError('接口解压后的响应超过 8 MB')
                if not body.strip():
                    data = {}
                else:
                    try:
                        data = json.loads(body.decode('utf-8-sig'))
                    except (ValueError, UnicodeDecodeError):
                        raise BmcError('接口没有返回 JSON；请确认这是 BMC HTTPS 地址，而不是监控平台地址') from None
                if isinstance(data, dict) and ('error' in data or str(data.get('status', '')).lower() == 'error'):
                    raise BmcError('设备在响应中报告错误；请核对认证方式和账号权限')
                return data, response.headers
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ApiError(exc.code, path) from None
        except ssl.SSLCertVerificationError:
            raise CertificateError('证书验证失败；若确认是自己的 BMC 自签名证书，请勾选“允许自签名证书”后重试') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise CertificateError('证书验证失败；若确认是自己的 BMC 自签名证书，请勾选“允许自签名证书”后重试') from None
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise BmcError('连接超时，请检查内网 / VPN 连接及 BMC 地址') from None
            detail = type(exc.reason).__name__
            number = getattr(exc.reason, 'winerror', None) or getattr(exc.reason, 'errno', None)
            if number is not None:
                detail += f' {number}'
            raise BmcError(f'无法直连 BMC [{detail}]；阶段：{self.phase}。请检查内网 / VPN、目标 IP、HTTPS 端口和设备状态') from None
        except (socket.timeout, TimeoutError):
            raise BmcError('请求超时，设备可能繁忙或网络不通') from None
        except http.client.HTTPException as exc:
            raise BmcError(f'设备返回的 HTTP 响应不完整或格式异常 [{type(exc).__name__}]；阶段：{self.phase}。若会话登录失败，可手动选择 HTTP Basic 单台验证') from None
        except EOFError:
            raise BmcError(f'设备返回的压缩响应不完整 [EOFError]；阶段：{self.phase}') from None
        except (ConnectionError, OSError) as exc:
            detail = type(exc).__name__
            number = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
            if number is not None:
                detail += f' {number}'
            raise BmcError(f'网络连接中断 [{detail}]；阶段：{self.phase}。请检查 BMC 和网络状态') from None

    def login(self, username, password, method='session'):
        # Some BMCs protect the service root; authenticate before reading it.
        if method == 'basic':
            encoded = base64.b64encode((username + ':' + password).encode('utf-8')).decode('ascii')
            self.headers['Authorization'] = 'Basic ' + encoded
            self.root, _ = self.request('GET', '/redfish/v1/')
            if not isinstance(self.root, dict):
                raise BmcError('Redfish 服务根返回的不是对象')
            self.credential_verified = True
            return
        if method != 'session':
            raise BmcError('未知认证方式')
        data, headers = self.request('POST', SESSION_PATH, {'UserName': username, 'Password': password})
        location = headers.get('Location') or (data.get('@odata.id') if isinstance(data, dict) else None)
        if location:
            location = self.safe_path(location)
            if re.fullmatch(re.escape(SESSION_PATH) + r'/[^/?]+', location):
                self.session_uri = location
        token = headers.get('X-Auth-Token')
        if token:
            self.headers['X-Auth-Token'] = token
        elif not list(self.jar):
            raise BmcError('设备未返回会话令牌或 Cookie；可手动切换 HTTP Basic 后再试')
        self.credential_verified = True
        self.root, _ = self.request('GET', '/redfish/v1/')
        if not isinstance(self.root, dict):
            raise BmcError('Redfish 服务根返回的不是对象')


    def close(self):
        warning = ''
        if self.session_uri:
            try:
                self.timeout = min(self.timeout, 5)
                self.request('DELETE', self.session_uri)
            except BmcError:
                warning = '本次会话注销未确认，等待设备按超时策略回收'
        self.headers.pop('Authorization', None)
        self.headers.pop('X-Auth-Token', None)
        self.jar.clear()
        return warning
