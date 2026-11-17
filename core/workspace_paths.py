"""Repository-local, ignored runtime paths; never search a parent site workspace."""
from pathlib import Path
WORKSPACE=Path(__file__).resolve().parent.parent
SOURCE_DIR=WORKSPACE/'var'/'reference'
FINAL_DIR=WORKSPACE/'var'/'exports'
RUNS_DIR=WORKSPACE/'var'/'runs'
ARCHIVE_DIR=WORKSPACE/'var'/'artifacts'
PACKAGE_DIR=WORKSPACE/'dist'
MANUAL_REPORT_DIR=RUNS_DIR/'manual'
GUI_TEST_DIR=ARCHIVE_DIR/'browser'
def default_inputs():
    return SOURCE_DIR/'baseline.xlsx',SOURCE_DIR/'source_sn.xlsx'
def default_master():
    return FINAL_DIR/'inventory.xlsx'
