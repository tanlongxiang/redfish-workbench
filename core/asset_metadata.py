"""Dependency-free asset metadata validation shared by CLI and Web storage.
The HTTP API may additionally validate the same fields with Pydantic.
"""
class AssetMetadata:
    limits = {'declared_sn':200, 'declared_model':200, 'location':200, 'tags':300, 'notes':1000}
    def __init__(self, **values):
        allowed = set(self.limits) | {'lifecycle'}
        if set(values) - allowed: raise ValueError('台账包含未知字段')
        self.data = {}
        for key, maximum in self.limits.items():
            value = values.get(key, '')
            if not isinstance(value, str) or len(value) > maximum: raise ValueError('台账字段类型或长度无效：'+key)
            self.data[key] = value
        lifecycle = values.get('lifecycle', 'active')
        if lifecycle not in ('active','spare','repair','retired'): raise ValueError('资产状态无效')
        self.data['lifecycle'] = lifecycle
    @classmethod
    def model_validate(cls, values):
        if not isinstance(values, dict): raise ValueError('台账元信息必须为对象')
        return cls(**values)
    def model_dump(self):
        return dict(self.data)
