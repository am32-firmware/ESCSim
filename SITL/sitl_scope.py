"""Compatibility imports for the shared simulator UI."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from escsim.control.scope import SIGNALS, ScopeCapture, ScopeFrame, diode, signal_value  # noqa: F401,E402
