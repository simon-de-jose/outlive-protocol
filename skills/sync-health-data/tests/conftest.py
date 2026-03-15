"""Import shared fixtures from top-level conftest."""
import sys
from pathlib import Path

# Ensure top-level tests/ conftest is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "tests"))
from conftest import *  # noqa: F401, F403
