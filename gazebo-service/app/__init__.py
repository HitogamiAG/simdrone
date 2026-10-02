"""Gazebo API application package."""
import sys
from pathlib import Path

_workspace = Path(__file__).resolve().parents[2]
for _module_root in (Path("/opt/uav/third-party"), _workspace / "third-party"):
    if _module_root.is_dir() and str(_module_root) not in sys.path:
        sys.path.insert(0, str(_module_root))
del _workspace, _module_root
