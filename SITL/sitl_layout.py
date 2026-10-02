"""Compatibility imports for the shared simulator UI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from escsim.control.layout import fit_window, scroll_panel, tile_windows  # noqa: F401,E402
