"""Compatibility entry point: domain taxonomy replaced by scopes/tags."""
import runpy
from pathlib import Path
globals().update(runpy.run_path(str(Path(__file__).with_name("test_taxonomy.py"))))
