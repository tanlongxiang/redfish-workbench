"""Read local BMC credentials as data; never source/execute a .env file."""
from __future__ import annotations
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / '.env'
KEYS = {'BMC_USERNAME', 'BMC_PASSWORD'}


def read_env(filename=None):
    path = Path(filename) if filename is not None else ENV_FILE
    if not path.exists():
        return {}
    values = {}
    for number, raw in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, value = line.partition('=')
        key = key.strip()
        if key not in KEYS:
            continue
        if not separator:
            raise ValueError(f'.env第{number}行缺少等号')
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'.env第{number}行引号未闭合')
            value = value[1:-1]
        values[key] = value
    return values


def load_credentials(username=None, *, filename=None, environ=None):
    """CLI username > process environment > local file; no env mutation."""
    values = read_env(filename)
    environment = os.environ if environ is None else environ
    for key in KEYS:
        if key in environment:
            values[key] = environment[key]
    selected_user = username if username is not None else values.get('BMC_USERNAME', '')
    return selected_user.strip(), values.get('BMC_PASSWORD', '')
