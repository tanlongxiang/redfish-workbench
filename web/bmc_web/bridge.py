"""Import the existing trusted engine; do not import any Tk GUI or execute uploads."""
import sys
from pathlib import Path
CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
from bmc_client import BmcError, normalize_target
from firmware_batch import (COMPONENTS, Image, Options, Target, WriteApproval, UpgradeClient, run_batch,
                            parse_targets, import_targets, version)
from firmware_demo import DemoClient, DEMO_MARKER
from firmware_readiness import ReadinessGate
from local_config import load_credentials

from bmc_client import BmcClient, ApiError, serial_text
from node_verify import FIRMWARE, has_sn
from inventory_merge import usable_firmware
from xlsx_io import InputBook, write_xlsx
