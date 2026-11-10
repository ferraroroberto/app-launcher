"""A small TOML writer for the documents the launcher edits and re-saves.

``tomllib`` only reads, and the launcher takes no third-party TOML package, so
this writes back what ``tomllib.loads`` produced: tables, arrays of tables,
arrays, strings, numbers, booleans and dates. Comments and the original layout
are not kept, because ``tomllib`` does not return them; a caller that edits a
file in place keeps a backup. :func:`dumps` output always parses back to an
equal document, and callers check that before they replace a file.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from typing import Any, Dict, List, Mapping, Sequence

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


class TomlWriteError(ValueError):
    """The document holds a value TOML cannot express."""


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else _string(name)


def _string(text: str) -> str:
    out: List[str] = []
    for char in text:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append("\\u%04X" % ord(char))
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)


def _inline(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return "[" + ", ".join(_inline(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_key(str(k))} = {_inline(v)}" for k, v in value.items()) + "}"
    raise TomlWriteError(f"cannot write a {type(value).__name__} value")


def _emit(lines: List[str], path: List[str], table: Mapping[str, Any], array_item: bool) -> None:
    if path:
        header = ".".join(_key(part) for part in path)
        if lines:
            lines.append("")
        lines.append(f"[[{header}]]" if array_item else f"[{header}]")
    nested = []
    for name, value in table.items():
        if isinstance(value, Mapping) or _is_table_array(value):
            nested.append((name, value))
        else:
            lines.append(f"{_key(str(name))} = {_inline(value)}")
    for name, value in nested:
        if isinstance(value, Mapping):
            _emit(lines, path + [str(name)], value, False)
        else:
            for item in value:
                _emit(lines, path + [str(name)], item, True)


def dumps(data: Dict[str, Any]) -> str:
    """The TOML text of ``data``: top-level values first, then each table in order."""
    lines: List[str] = []
    _emit(lines, [], data, False)
    return "\n".join(lines) + "\n"
