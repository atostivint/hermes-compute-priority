"""Make the plugin root importable when pytest runs from anywhere.

`tests/` has no `__init__.py`, so pytest puts `tests/` on `sys.path`, not the repository root. The
core module lives at the root, hence this shim.
"""

from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
