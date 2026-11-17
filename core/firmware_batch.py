"""Batch DCU CPLD maintenance. Explicit write adapter, separate from read-only client.
No network activity on import. No automatic shutdown, reboot, retry or resume.
"""
from __future__ import annotations
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bmc_client import BmcClient, BmcError, ApiError, normalize_target
from firmware_auth import confirmation_key
from inventory_merge import usable_firmware
from node_verify import node_address
from workspace_paths import RUNS_DIR
from xlsx_io import InputBook, write_xlsx

COMPONENTS = ('Dcu02Cpld', 'Dcu13Cpld', 'Dcu46Cpld', 'Dcu57Cpld')
CPLD = '/xyz/openbmc_project/software/cpld'
INVENTORY = '/redfish/v1/UpdateService/FirmwareInventory/'
REQUEST_ACTIVE = 'xyz.openbmc_project.Software.Activation.RequestedActivations.Active'
MAX_IMAGE = 64 * 1024 * 1024
BUSY = {'Activating', 'Updating', 'Downloading', 'Staging'}
TERMINAL_OK = {'Active'}
LOCAL_LOCKS = set()
LOCAL_LOCK_GUARD = threading.Lock()


def version(value):
    text = str(value).strip()
    if not usable_firmware(text):
        raise BmcError('固件版本为空或为无效占位值')
    return re.sub(r'^v(?=\d)', '', text, flags=re.I)


def scalar(value):
    return str(value)[:200] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ''


@dataclass(frozen=True)
class Target:
    name: str
    address: str
    def __post_init__(self):
        object.__setattr__(self, 'address', normalize_target(self.address))
        if not self.name or len(self.name) > 100 or any(ord(c) < 32 for c in self.name):
            raise ValueError('节点名为空、过长或含控制字符')


def validate_targets(targets):
    if not targets:
        raise ValueError('请至少选择一个节点')
    if len({t.address for t in targets}) != len(targets) or len({t.name for t in targets}) != len(targets):
        raise ValueError('节点名或 BMC 地址重复；拒绝对同一 BMC 并行操作')
    return tuple(targets)


def parse_targets(text):
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = re.split(r'[,;\s]+', line)
        if len(parts) == 1:
            item = parts[0]
            try:
                ip = node_address(item)
            except ValueError:
                ip = item
            result.append(Target(item, ip))
        elif len(parts) == 2:
            name, ip = parts
            if re.fullmatch(r'[a-hjkmn]\d{2}r[1-4]n\d{2}', name.lower()):
                if normalize_target(node_address(name.lower())) != normalize_target(ip):
                    raise ValueError('节点名与 BMC 地址映射不一致：' + name)
            result.append(Target(name, ip))
        else:
            raise ValueError('每行填写节点名、IP，或“节点名 IP”')
    return list(validate_targets(result))


def import_targets(filename):
    book = InputBook(str(filename))
    result = []
    for sheet in book.sheets:
        rows = [(n, row) for n, row in book.read_sheet(sheet) if any(str(x).strip() for x in row)]
        if not rows:
            continue
        headers = [str(x).strip().lower() for x in rows[0][1]]
        name_col = next((i for i, x in enumerate(headers) if x in ('节点名', 'node', 'name')), None)
        ip_col = next((i for i, x in enumerate(headers) if x in ('bmc_ip', 'bmcip', 'bmc ip', 'ip', 'bmc地址')), None)
        if ip_col is None:
            raise ValueError('工作表缺少 BMC_IP / IP 列：' + sheet)
        for _, row in rows[1:]:
            address = row[ip_col].strip() if ip_col < len(row) else ''
            name = row[name_col].strip() if name_col is not None and name_col < len(row) else address
            if not name and not address:
                continue
            if not address or not name:
                raise ValueError('清单有缺少节点名或地址的记录')
            result.extend(parse_targets(name + ' ' + address))
    return list(validate_targets(result))


@dataclass(frozen=True)
class Image:
    component: str
    path: Path
    expected: str
    sha256: str
    vendor: str = 'Lattice'

    @classmethod
    def prepare(cls, component, filename, expected, vendor='Lattice'):
        if component not in COMPONENTS:
            raise ValueError('本版仅支持四组 DCU CPLD')
        path = Path(filename).resolve()
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,160}', path.name):
            raise ValueError('镜像文件名仅支持 ASCII 字母、数字、点、短横线和下划线，最长 160 字符')
        if path.suffix.lower() not in ('.svf', '.tar'):
            raise ValueError('CPLD 镜像仅接受 .svf / .tar（与已检查网页一致）')
        if vendor != 'Lattice' or 'LT' not in path.name:
            raise ValueError('本适配器只支持 Lattice CPLD，文件名须含 LT；这不能代替镜像型号核对')
        if not 0 < path.stat().st_size <= MAX_IMAGE:
            raise ValueError('镜像为空或超过 64 MiB')
        if expected.strip():
            version(expected)
        return cls(component, path, expected.strip(), hashlib.sha256(path.read_bytes()).hexdigest(), vendor)

    def read_verified(self):
        # A single bounded snapshot is both hashed and uploaded, avoiding file TOCTOU.
        with self.path.open('rb') as f:
            raw = f.read(MAX_IMAGE + 1)
        if not raw or len(raw) > MAX_IMAGE or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise BmcError('镜像自计划生成后发生变化，已阻止上传；请重新生成计划')
        return raw


class WriteApproval:
    """One-run, exact-scope permit. Created only after an operator confirms the GUI.

    This prevents accidental writes; it is not an OS security boundary against a
    person who can edit/run arbitrary Python. Never mint this on a user's behalf.
    """
    @staticmethod
    def signature(targets, images, model, action):
        data = {'action': action, 'model': model,
                'targets': [(t.name, t.address) for t in targets],
                'images': [(i.component, i.path.name, i.expected, i.sha256) for i in images]}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def __init__(self, targets, images, model, action):
        if action not in ('upgrade', 'GracefulShutdown', 'ForceOff', 'On'):
            raise ValueError('许可动作无效')
        self.signature_ = self.signature(targets, images, model, action)
        self.addresses = frozenset(t.address for t in targets)
        self.images = {i.component: i for i in images}
        self.action = action
        self.created = time.monotonic()
        self.claimed = False
        self.lock = threading.Lock()

    def claim(self, targets, images, model, action):
        with self.lock:
            if self.claimed or time.monotonic() - self.created > 600:
                raise ValueError('本次写入许可已使用或过期，请操作者重新确认')
            if self.signature_ != self.signature(targets, images, model, action):
                raise ValueError('节点、机型、动作或镜像与确认范围不一致')
            self.claimed = True

    def allows(self, address, method, path, payload, binary):
        if not self.claimed or address not in self.addresses:
            return False
        if self.action != 'upgrade':
            return method == 'POST' and isinstance(payload, dict) and payload == {'ResetType': self.action}
        if method == 'POST' and binary and isinstance(payload, bytes):
            return any(path == '/upload/image/' + urllib.parse.quote(i.path.name, safe='') and
                       hashlib.sha256(payload).hexdigest() == i.sha256 for i in self.images.values())
        if method == 'PUT' and isinstance(payload, dict):
            if path == CPLD + '/attr/ExtendedVersion':
                return payload.get('data') in self.images
            return path == CPLD + '/attr/RequestedActivation' and payload == {'data': REQUEST_ACTIVE}
        return False


@dataclass(frozen=True)
class Options:
    username: str
    password: str = field(repr=False)
    model: str = ''
    auth: str = 'basic'
    self_signed: bool = False
    timeout: int = 20
    flash_timeout: int = 900
    poll: float = 3
    workers: int = 2
    canary_confirmed: bool = False
    approval: WriteApproval | None = field(default=None, repr=False, compare=False)
    def __post_init__(self):
        if not self.username or not self.password:
            raise ValueError('请填写 BMC 账号和密码')
        if self.auth not in ('basic', 'session') or not 1 <= self.workers <= 4:
            raise ValueError('认证方式无效或并发不在 1～4 范围')
        if not 1 <= self.timeout <= 120 or not 1 <= self.flash_timeout <= 3600 or not 0 < self.poll <= 30:
            raise ValueError('请求/升级超时或轮询间隔超出范围')


class Journal:
    def __init__(self, folder=None):
        base = Path(folder) if folder else RUNS_DIR / '固件升级'
        base.mkdir(parents=True, exist_ok=True)
        self.folder = base / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.folder.mkdir(mode=0o700)
        self.path = self.folder / 'events.jsonl'
        self.lock = threading.Lock()

    def add(self, target, event, **data):
        # Callers supply only explicitly selected fields; never responses/headers/credentials.
        row = {'time': datetime.now().astimezone().isoformat(), 'node': target.name,
               'address': target.address, 'event': event, **data}
        with self.lock, self.path.open('a', encoding='utf-8') as f:
            os.chmod(self.path, 0o600)
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())


class NodeLock:
    """Cross-thread/process lock; stale crash locks must be investigated, never stolen."""
    def __init__(self, address):
        self.address = address
        directory = RUNS_DIR / '固件升级' / 'locks'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (hashlib.sha256(address.encode()).hexdigest() + '.lock')
    def __enter__(self):
        with LOCAL_LOCK_GUARD:
            if self.address in LOCAL_LOCKS:
                raise BmcError('本节点已有维护任务')
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise BmcError('本节点存在维护锁；请核实其他工具实例或上次未完成任务') from None
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            LOCAL_LOCKS.add(self.address)
        return self
    def __exit__(self, *args):
        with LOCAL_LOCK_GUARD:
            LOCAL_LOCKS.discard(self.address)
            self.path.unlink(missing_ok=True)


class UpgradeClient(BmcClient):
    """Reuses isolated HTTPS session, but never relaxes BmcClient.request's whitelist."""
    def __init__(self, target, options):
        super().__init__(target, options.timeout, options.self_signed)
        self.options = options
        self.reset_path = None
        self.allowed_resets = set()

    def write(self, method, path, payload, *, binary=False):
        path = self.safe_path(path)
        approval = self.options.approval
        if not isinstance(approval, WriteApproval) or not approval.allows(self.base, method, path, payload, binary):
            raise BmcError('真实写入默认禁止：缺少操作者对本次具体节点、动作和镜像的有效确认')
        upload = method == 'POST' and re.fullmatch(r'/upload/image/[^/?]+', path)
        attr = method == 'PUT' and path in (CPLD + '/attr/ExtendedVersion', CPLD + '/attr/RequestedActivation')
        reset = method == 'POST' and self.reset_path and path == self.reset_path
        if not (upload or attr or reset):
            raise BmcError('升级客户端拒绝未列入白名单的写操作')
        if upload and isinstance(payload, bytes) and payload.startswith(b'BMC-DEMO-DO-NOT-FLASH\n'):
            raise BmcError('演示镜像禁止上传到真实 BMC')
        if upload and (not binary or not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_IMAGE):
            raise BmcError('镜像上传参数不正确')
        if attr:
            allowed = COMPONENTS if path.endswith('ExtendedVersion') else (REQUEST_ACTIVE,)
            if not isinstance(payload, dict) or set(payload) != {'data'} or payload['data'] not in allowed:
                raise BmcError('CPLD 属性值不在允许列表中')
        if reset and (not isinstance(payload, dict) or set(payload) != {'ResetType'} or
                      payload['ResetType'] not in self.allowed_resets or
                      payload['ResetType'] not in ('GracefulShutdown', 'ForceOff', 'On')):
            raise BmcError('设备未明确公布该电源动作，已阻止发送')
        headers = {**self.headers, 'Accept-Encoding': 'identity', 'AUTHKEY': confirmation_key(self.options.password)}
        for cookie in self.jar:
            if cookie.name == 'XSRF-TOKEN':
                headers['X-XSRF-TOKEN'] = urllib.parse.unquote(cookie.value)
        headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
        body = payload if binary else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        # A write timeout is an unknown outcome, NOT permission to retry it.
        timeout = max(self.timeout, 600) if upload else max(self.timeout, 50)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise BmcError('写接口响应过大，结果待人工核验；不会重试')
                if raw.strip():
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise BmcError('写接口未返回可识别 JSON，结果待核验；不会重试') from None
                    if not isinstance(data, dict) or 'error' in data or str(data.get('status', '')).lower() == 'error':
                        raise BmcError('写接口报告错误；不自动重试，请核验设备状态')
                return
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise BmcError(f'写接口 HTTP {status}；若为 401/403，请核对写权限、会话及二次确认；不会重试') from None
        except (urllib.error.URLError, OSError, socket.timeout, http.client.HTTPException):
            raise BmcError('写请求连接中断/超时，执行结果未知；请现场核验，禁止自动重发') from None

    def system(self):
        link = self.root.get('Systems', {}).get('@odata.id')
        if not link:
            raise BmcError('没有 Systems 链接')
        collection, _ = self.request('GET', link)
        members = collection.get('Members', [])
        if (collection.get('Members@odata.nextLink') or collection.get('@odata.nextLink') or
            len(members) != 1 or collection.get('Members@odata.count', 1) != 1):
            raise BmcError('Systems 不是唯一且完整的单节点集合，不猜测目标')
        uri = self.safe_path(members[0]['@odata.id'])
        body, _ = self.request('GET', uri)
        action = body.get('Actions', {}).get('#ComputerSystem.Reset', {})
        self.reset_path = None
        self.allowed_resets = set(action.get('ResetType@Redfish.AllowableValues', []))
        if action.get('target'):
            path = self.safe_path(action['target'])
            if path == uri.rstrip('/') + '/Actions/ComputerSystem.Reset' and '?' not in path:
                self.reset_path = path
        return body

    def cpld(self):
        obj, _ = self.request('GET', CPLD)
        if not isinstance(obj, dict) or not isinstance(obj.get('data'), dict):
            raise BmcError('不兼容的旧式 CPLD 接口')
        return obj['data']


def power_snapshot(client):
    started = time.monotonic()
    system = client.system()
    return {'kind': 'power', 'model': scalar(system.get('Model')), 'power': scalar(system.get('PowerState')),
            'inventory': {}, 'reset_types': sorted(client.allowed_resets),
            'collection_duration_seconds': time.monotonic() - started}


def snapshot(client, components=COMPONENTS):
    started = time.monotonic()
    system = client.system()
    cpld = client.cpld()
    inventory = {}
    for component in components:
        body, _ = client.request('GET', INVENTORY + component)
        inventory[component] = {
            'version': scalar(body.get('Version')),
            'updateable': body.get('Updateable') is True,
            'state': scalar(body.get('Status', {}).get('State')),
        }
    return {'model': scalar(system.get('Model')), 'power': scalar(system.get('PowerState')),
            'activation': scalar(cpld.get('Activation')).split('.')[-1],
            'progress': cpld.get('Progress') if isinstance(cpld.get('Progress'), (int, float)) else None,
            'target': scalar(cpld.get('ExtendedVersion')), 'vendors': cpld.get('VendorList', {}),
            'effective': cpld.get('EffectiveList', []), 'inventory': inventory,
            'reset_types': sorted(client.allowed_resets),
            'collection_duration_seconds': time.monotonic() - started}


def check(snapshot_, images, options):
    # Legacy callers may request an explicit model filter; blank means no model gate.
    if options.model.strip() and snapshot_['model'] != options.model.strip():
        raise BmcError('设备机型与显式指定的机型不一致')
    if snapshot_['power'] != 'Off':
        raise BmcError('主机未明确处于 Off；请单独关机并重新检查，不自动强制关机')
    if snapshot_['activation'] not in TERMINAL_OK:
        raise BmcError('CPLD 当前状态不是 Active；已有任务或异常待处理')
    effective = {entry[0]: entry[1:] for entry in snapshot_['effective'] if isinstance(entry, list) and len(entry) == 3}
    for image in images:
        item = snapshot_['inventory'][image.component]
        if snapshot_['vendors'].get(image.component) != image.vendor:
            raise BmcError('CPLD 厂商不匹配：' + image.component)
        if effective.get(image.component) != ['gpio', 'poweroff']:
            raise BmcError('该节点生效条件不同于已支持的 gpio/poweroff：' + image.component)
        if not item['updateable'] or item['state'] not in ('Enabled', 'StandbyOffline'):
            raise BmcError('组件不在位或没有明确的可更新能力：' + image.component)
        version(item['version'])


def wait_flash(client, image, before, options, notify):
    deadline = time.monotonic() + options.flash_timeout
    observed_new_operation = False
    while time.monotonic() < deadline:
        obj = client.cpld()
        state = scalar(obj.get('Activation')).split('.')[-1]
        progress = obj.get('Progress')
        if scalar(obj.get('ExtendedVersion')) != image.component:
            raise BmcError('目标被其他操作者更改，停止后续操作；本次烧写状态待核验')
        if state not in TERMINAL_OK | BUSY:
            raise BmcError('CPLD 激活失败或返回未知状态：' + state)
        if state in BUSY or (isinstance(progress, (int, float)) and 0 <= progress < 100):
            observed_new_operation = True
        notify('烧写中', component=image.component, progress=progress if isinstance(progress, (int, float)) else None)
        if state in TERMINAL_OK and progress == 100:
            observed = obj.get('VersionList', {}).get(image.component)
            changed = bool(observed and (not image.expected or version(observed) == version(image.expected)) and
                           version(before) != version(observed))
            if observed_new_operation or changed:
                return
        time.sleep(options.poll)
    raise BmcError('升级监控超时/未能证明是本次完成（可能是残留 100%）；不自动重刷')


def maintain_one(target, images, options, action, stop, journal, callback, client_class=UpgradeClient):
    results = []
    def emit(event, **data):
        try:
            journal.add(target, event, **data)
        except OSError:
            stop.set()
            raise
        if callback:
            callback(target, event, data)
    client = None
    try:
        if stop.is_set():
            return {'node': target.name, 'address': target.address, 'status': '已停止，未连接', 'components': []}
        with NodeLock(target.address):
            client = client_class(target.address, options)
            client.login(options.username, options.password, options.auth)
            if action in ('GracefulShutdown', 'ForceOff', 'On', 'power-read'):
                snap = power_snapshot(client)
                emit('电源读取', snapshot=snap)
                if action == 'power-read':
                    return {'node': target.name, 'address': target.address, 'status': '电源信息已读取（只读）', 'snapshot': snap, 'components': []}
                if options.model.strip() and snap['model'] != options.model.strip():
                    raise BmcError('设备机型与显式指定的机型不一致')
                if snap['power'] not in ('On', 'Off'):
                    raise BmcError('未能确认当前主机电源状态，禁止发送电源请求')
                desired = 'On' if action == 'On' else 'Off'
                if snap['power'] == desired:
                    return {'node': target.name, 'address': target.address, 'status': '已处于 ' + desired, 'components': []}
                if stop.is_set():
                    raise BmcError('已停止，未发送电源请求')
                if not client.reset_path or action not in client.allowed_resets:
                    raise BmcError('设备未明确公布兼容电源动作；不猜测接口，请先用原网页完成该步骤')
                emit('发送电源请求前', action=action)
                client.write('POST', client.reset_path or '', {'ResetType': action})
                deadline = time.monotonic() + options.flash_timeout
                while time.monotonic() < deadline:
                    if client.system().get('PowerState') == desired:
                        emit('电源状态确认', power=desired)
                        return {'node': target.name, 'address': target.address, 'status': '已确认 ' + desired, 'components': []}
                    time.sleep(options.poll)
                raise BmcError('等待电源状态超时，不自动转为强制关机')
            selected = tuple(i.component for i in images) if images else COMPONENTS
            snap = snapshot(client, selected)
            emit('预检查', snapshot=snap)
            if action in ('precheck', 'verify'):
                if action == 'precheck':
                    if images:
                        check(snap, images, options)
                    state = '预检查通过' if images else '信息已读取（只读，未检查升级条件）'
                else:
                    if not images:
                        raise BmcError('版本读回需要组件计划')
                    if options.model.strip() and snap['model'] != options.model.strip():
                        raise BmcError('核验机型与计划机型不一致')
                    for image in images:
                        actual = snap['inventory'][image.component]['version']
                        version(actual)
                        if image.expected and version(actual) != version(image.expected):
                            raise BmcError('版本尚未匹配目标：' + image.component)
                        results.append({'component':image.component,'expected':image.expected,'observed':actual,
                                        'status':'版本读回一致' if image.expected else '版本已读回，需人工核验'})
                    state = ('版本读回一致（仍需确认启动及硬件状态）' if all(i.expected for i in images)
                             else '版本已读回（未提供预期版本，需人工核验）')
                return {'node': target.name, 'address': target.address, 'status': state, 'snapshot': snap, 'components': results}
            if action != 'upgrade':
                raise ValueError('未知维护动作')
            check(snap, images, options)
            for image in images:
                if stop.is_set():
                    return {'node': target.name, 'address': target.address, 'status': '已停止后续组件', 'components': results}
                # Re-read before every image: shared staging/activation belongs to this BMC.
                fresh = snapshot(client, selected)
                check(fresh, images, options)
                before = fresh['inventory'][image.component]['version']
                if image.expected and version(before) == version(image.expected):
                    results.append({'component': image.component, 'before': before, 'expected': image.expected, 'status': '目标版本已存在，跳过'})
                    emit('跳过同版本', component=image.component, version=before)
                    continue
                raw = image.read_verified()
                emit('上传前', component=image.component, sha256=image.sha256, filename=image.path.name,
                     before=before, expected=image.expected)
                client.write('POST', '/upload/image/' + urllib.parse.quote(image.path.name, safe=''), raw, binary=True)
                del raw
                emit('上传完成', component=image.component)
                if stop.is_set():
                    raise BmcError('已停止后续操作；镜像已上传但未激活，请人工核验暂存区')
                if client.system().get('PowerState') != 'Off':
                    raise BmcError('上传后主机未确认 Off，禁止选择目标和激活；请人工核验暂存区')
                emit('选择目标前', component=image.component)
                client.write('PUT', CPLD + '/attr/ExtendedVersion', {'data': image.component})
                if scalar(client.cpld().get('ExtendedVersion')) != image.component:
                    raise BmcError('目标选择读回不一致，禁止激活')
                if stop.is_set():
                    raise BmcError('已停止，目标已选择但未激活；请人工核验暂存区')
                if client.system().get('PowerState') != 'Off':
                    raise BmcError('激活前主机未确认 Off，禁止激活；请人工核验已上传镜像')
                emit('激活前', component=image.component)
                client.write('PUT', CPLD + '/attr/RequestedActivation', {'data': REQUEST_ACTIVE})
                # Once activated, stop only affects NEXT component/node. Do not interrupt flash.
                wait_flash(client, image, before, options, emit)
                item = {'component': image.component, 'before': before, 'expected': image.expected,
                        'sha256': image.sha256, 'status': '写入完成，待生效核验'}
                results.append(item)
                emit('写入完成，待生效核验', **item)
            state = '写入完成，待生效核验' if any(r['status'].startswith('写入') for r in results) else '目标版本已存在，未刷写'
            return {'node': target.name, 'address': target.address, 'status': state, 'components': results}
    except Exception as exc:
        # Never stringify arbitrary network/client exceptions that may contain credentials.
        error = str(exc) if isinstance(exc, (BmcError, ValueError)) else '内部/文件错误：' + type(exc).__name__
        for secret in (options.password, confirmation_key(options.password) if options.password.isascii() and 0 < len(options.password) <= 24 and '\0' not in options.password else ''):
            if secret:
                error = error.replace(secret, '[REDACTED]')
        emit('停止并待核验', reason=error)
        return {'node': target.name, 'address': target.address, 'status': '停止并待核验', 'error': error, 'components': results}
    finally:
        if client:
            try:
                warning = client.close()
            except Exception:
                warning = '未确认'
            if warning:
                emit('会话注销未确认')


def run_batch(targets, images, options, action='precheck', stop=None, callback=None, folder=None, client_class=UpgradeClient):
    targets = validate_targets(targets)
    images = tuple(images)
    if len({i.component for i in images}) != len(images):
        raise ValueError('升级计划有重复组件')
    if action not in ('precheck', 'verify', 'upgrade', 'GracefulShutdown', 'ForceOff', 'On', 'power-read'):
        raise ValueError('未知维护动作')
    if action == 'upgrade':
        if not images:
            raise ValueError('请先配置 CPLD 镜像')
        if len(targets) > 1 and not options.canary_confirmed:
            raise ValueError('多台升级前必须确认：同机型、同镜像已完成单台现场验证')
        confirmation_key(options.password)
        for image in images:
            raw = image.read_verified()
            if client_class is UpgradeClient and raw.startswith(b'BMC-DEMO-DO-NOT-FLASH\n'):
                raise ValueError('演示镜像禁止用于真实 BMC')
    is_live = issubclass(client_class, UpgradeClient)
    if is_live and action in ('upgrade', 'GracefulShutdown', 'ForceOff', 'On'):
        if not isinstance(options.approval, WriteApproval):
            raise ValueError('真实写操作未获本次明确许可；禁止连接和执行。仅开发工具不构成执行许可')
        options.approval.claim(targets, images, options.model, action)
    stop = stop if stop is not None else threading.Event()
    mode = '真实设备' if issubclass(client_class, UpgradeClient) else '模拟（无设备操作）'
    journal = Journal(folder)
    for target in targets:
        journal.add(target, '计划', action=action, mode=mode, model=options.model,
                    approval_scope=options.approval.signature_ if is_live and options.approval else None,
                    images=[{'component': i.component, 'filename': i.path.name, 'sha256': i.sha256, 'expected': i.expected} for i in images])
    results = []
    # Per-node exceptions halt that node's remaining components; other nodes remain independent.
    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        futures = [pool.submit(maintain_one, t, images, options, action, stop, journal, callback, client_class) for t in targets]
        for future in as_completed(futures):
            result = future.result()
            result['mode'] = mode
            results.append(result)
            if callback:
                callback(next(t for t in targets if t.name == result['node']), '节点结果', result)
    order = {t.name: i for i, t in enumerate(targets)}
    results.sort(key=lambda r: order[r['node']])
    headers = ['节点名', 'BMC_IP', '执行模式', '本次动作', '节点结果', '组件', '升级前版本', '预期版本（可空）', '实际读回版本', '组件结果', 'SHA256', '说明']
    rows = []
    for result in results:
        items = result['components'] or [{}]
        for item in items:
            rows.append([result['node'], result['address'], mode, action, result['status'], item.get('component', ''),
                         item.get('before', ''), item.get('expected', ''), item.get('observed', ''), item.get('status', ''),
                         item.get('sha256', ''), result.get('error', '')])
    write_xlsx(str(journal.folder / '维护结果.xlsx'), headers, rows)
    (journal.folder / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return results, journal.folder


def save_config(filename, targets, images, model):
    """Reusable local plan: no password, cookie, AUTHKEY or canary approval is saved."""
    targets = validate_targets(targets)
    data = {'format': 'bmc-cpld-plan-v1', 'model': model,
            'targets': [{'name': t.name, 'address': t.address} for t in targets],
            'images': [{'component': i.component, 'path': str(i.path), 'expected': i.expected,
                        'sha256': i.sha256, 'vendor': i.vendor} for i in images]}
    with Path(filename).open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config(filename):
    with Path(filename).open('rb') as f:
        raw = f.read(1024*1024 + 1)
    if len(raw) > 1024*1024:
        raise ValueError('配置文件超过 1 MiB')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('format') != 'bmc-cpld-plan-v1':
        raise ValueError('不是本工具的配置文件')
    if not isinstance(data.get('model'), str) or len(data['model']) > 200:
        raise ValueError('配置机型字段无效')
    if not isinstance(data.get('targets'), list) or not 0 < len(data['targets']) <= 10000:
        raise ValueError('配置节点数量无效')
    targets = validate_targets([Target(row['name'], row['address']) for row in data['targets']])
    # Preserve mapping validation for recognizable site node names.
    for target in targets:
        parse_targets(target.name + ' ' + target.address)
    if not isinstance(data.get('images'), list) or len(data['images']) > 4:
        raise ValueError('配置组件数量无效')
    images = []
    for row in data['images']:
        if not Path(row['path']).is_absolute():
            raise ValueError('镜像配置路径须为绝对路径；迁移后请重新选择镜像')
        image = Image.prepare(row['component'], row['path'], row['expected'], row['vendor'])
        if image.sha256 != row['sha256']:
            raise ValueError('镜像与保存配置的 SHA256 不一致；请重新核对镜像')
        images.append(image)
    if len({i.component for i in images}) != len(images):
        raise ValueError('配置包含重复组件')
    return targets, tuple(images), data['model']
