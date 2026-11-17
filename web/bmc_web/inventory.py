"""Web compatibility facade; the collector itself uses only the standard library."""
from . import bridge as _bridge
from asset_inventory import DemoInventoryClient, system_resource, collect_one, run_inventory
