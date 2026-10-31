"""Port of the part of shared/color.ts the server needs."""

from __future__ import annotations

import re
from typing import Any

_HEX = re.compile(r"#[0-9a-f]{6}", re.I | re.A)


def is_hex(s: Any) -> bool:
    return isinstance(s, str) and _HEX.fullmatch(s) is not None
