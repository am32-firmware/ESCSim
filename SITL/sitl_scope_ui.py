"""Compatibility imports for the shared simulator UI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from escsim.control.scope_ui import COLORS, DemagScopeWindow  # noqa: F401,E402
