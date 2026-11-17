#!/usr/bin/env python3
"""Source entrypoint. Release users can run the self-contained bmc_tool.pyz."""
from pathlib import Path
import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# The release source package contains _engine; development reuses the sibling core.
CORE = HERE/'_engine'
if not CORE.is_dir(): CORE = HERE.parent/'core'
sys.path.insert(0, str(CORE))
from bmc_cli.app import main
if __name__ == '__main__':
    raise SystemExit(main())
