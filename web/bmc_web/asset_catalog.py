"""Web compatibility facade; asset storage is shared with the stdlib CLI."""
from . import bridge as _bridge
from asset_catalog import Catalog, import_ledger, FIELDS, DEFAULT_META, STATES
