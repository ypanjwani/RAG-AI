"""Data built from the TypeScript sources by `npm run export:server`.

Prompts, output formats, tool definitions, schemas, menus and limits live in
TypeScript (prompts/, shared/) so they have one source; scripts/export-server.mjs
writes them to generated/*.json, which is committed. Never edit those files.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

GENERATED = Path(__file__).parent / "generated"


@cache
def _load(name: str) -> Any:
    return json.loads((GENERATED / f"{name}.json").read_text(encoding="utf-8"))


def prompt(name: str) -> str:
    return _load("prompts")[name]


def formats() -> dict[str, Any]:
    return _load("formats")


def schemas() -> dict[str, Any]:
    return _load("schemas")


def menus() -> dict[str, Any]:
    return _load("menus")


def menu(name: str) -> dict[str, Any]:
    return _load("menus")[name]


def constants() -> dict[str, Any]:
    return _load("constants")


def limits() -> dict[str, int]:
    return _load("constants")["LIMITS"]
