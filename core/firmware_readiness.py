"""Local UI readiness only: no HTTP, no write permission, no automatic action."""
from __future__ import annotations
from copy import deepcopy
import time
from firmware_batch import check, version
from bmc_client import BmcError


class ReadinessGate:
    def __init__(self, ttl=120):
        self.ttl = ttl
        self.observations = {}
        self.invalidate()

    def invalidate(self, clear_observations=False):
        self.plan_key = None
        self.passed = set()
        if clear_observations:
            self.observations.clear()

    def observe(self, address, snapshot, observed_at=None):
        self.observations[address] = (deepcopy(snapshot), time.monotonic() if observed_at is None else observed_at)
        self.invalidate()

    def commit(self, plan_key, results):
        # Only a finished, explicit upgrade precheck may enable the button.
        self.plan_key = plan_key
        self.passed = {r['address'] for r in results if r.get('status') == '预检查通过'}

    def evaluate(self, plan_key, targets, images, options, *, demo=False, now=None):
        now = time.monotonic() if now is None else now
        if not targets:
            return False, '未选择节点'
        if not images:
            return False, '未勾选 DCU 升级组'
        for target in targets:
            observation = self.observations.get(target.address)
            if observation is None:
                return False, f'{target.name} 未读取状态；请执行升级预检查'
            snap, observed_at = observation
            if now - observed_at > self.ttl or observed_at > now:
                return False, f'{target.name} 状态已过期；请重新执行升级预检查'
            if snap.get('power') != 'Off':
                return False, f'{target.name} 电源为 {snap.get("power") or "未知"}，未确认 Off'
            if snap.get('kind') == 'power':
                return False, f'{target.name} 仅确认电源 Off，尚未完成 CPLD 升级预检查'
            try:
                check(snap, images, options)
            except (BmcError, KeyError, TypeError, ValueError) as exc:
                reason = str(exc) if isinstance(exc, BmcError) else '组件信息不完整'
                return False, f'{target.name}：{reason}'
        if all(i.expected for i in images) and all(version(self.observations[t.address][0]['inventory'][i.component]['version']) == version(i.expected)
               for t in targets for i in images):
            return False, '所选节点和组件已全部是目标版本，无需升级'
        if self.plan_key != plan_key or any(t.address not in self.passed for t in targets):
            return False, '本批节点与当前镜像计划尚未全部通过升级预检查'
        if not demo and len(targets) > 1 and not options.canary_confirmed:
            return False, '多台升级须先确认同机型、同镜像已完成单台现场验证'
        oldest = max(int(now - self.observations[t.address][1]) for t in targets)
        return True, f'本批预检查通过，全部节点已确认 Off；最早状态距今 {oldest} 秒。执行前仍会再次读取电源状态'
