"""Development app factory used by uvicorn reload mode."""
import os
from pathlib import Path

from .app import create_app
from .service import Settings

_data_dir=Path(os.environ.get('REDFISH_DATA_DIR', 'var/web')).resolve()
_origin=os.environ.get('REDFISH_ORIGIN', 'http://127.0.0.1:38080')
app=create_app(Settings(_data_dir, origin=_origin))
