"""The migration declaration: ``[install.migrate]`` and ``[install.endpoint]`` of a ``.fleet.toml``.

Vendored verbatim (manifest key ``installer_declaration``), beside ``src/installer_migrate.py``,
which loads it by path from its own folder and re-exports every name, so callers read
``installer_migrate.parse_migration`` and the rest. Stdlib only, no side effects: it only
parses the declaration and resolves the port this PC's config sets (schema: project-scaffolding
``docs/installer.md``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_START = ["tray.bat", "--restart"]
DEFAULT_VERSION_PATH = "/api/version"
DEFAULT_PORT_KEY = "port"
SHARED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass
class Endpoint:
    """Where the app answers ``{git_sha, built_at}``; always asked on loopback."""

    scheme: str = "http"
    port: int = 0
    version_path: str = DEFAULT_VERSION_PATH
    self_signed: bool = False  # accept any certificate, which is safe only because the host is 127.0.0.1
    port_config: str = ""  # a repo-relative JSON config whose `port_key` overrides `port` on this PC
    port_key: str = DEFAULT_PORT_KEY


@dataclass
class Migration:
    """``[install.migrate]`` and ``[install.endpoint]`` of a ``.fleet.toml``."""

    copy: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)
    start: list[str] = field(default_factory=lambda: list(DEFAULT_START))
    endpoint: Endpoint | None = None


def _relative_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{where} {key} must be a list of non-empty strings")
    return [_repo_path(item, f"{where} {key} entry") for item in value]


def _repo_path(item: str, what: str) -> str:
    """``item`` as a forward-slash repo-relative path; ValueError when it points outside the repo."""
    parts = Path(item.replace("\\", "/")).parts
    if Path(item).is_absolute() or ".." in parts or re.match(r"^[A-Za-z]:", item):
        raise ValueError(f"{what} {item!r} must be a path inside the repo")
    return item.replace("\\", "/").rstrip("/")


def _port(value: Any) -> int:
    text = str(value).strip().lstrip(":")
    if isinstance(value, bool) or not text.isdigit() or not 0 < int(text) < 65536:
        raise ValueError("[install.endpoint] port must be a TCP port")
    return int(text)


def parse_endpoint(table: Any, top_port: Any) -> Endpoint | None:
    """``[install.endpoint]``; its port falls back to the top-level ``port``. No table is None."""
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ValueError("[install.endpoint] must be a table")
    scheme = table.get("scheme", "http")
    if scheme not in ("http", "https"):
        raise ValueError('[install.endpoint] scheme must be "http" or "https"')
    if "port" not in table and top_port is None:
        raise ValueError("[install.endpoint] needs a port (or a top-level port)")
    path = table.get("version_path", DEFAULT_VERSION_PATH)
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError('[install.endpoint] version_path must start with "/"')
    self_signed = table.get("self_signed", False)
    if not isinstance(self_signed, bool):
        raise ValueError("[install.endpoint] self_signed must be true or false")
    port_config = table.get("port_config", "")
    if not isinstance(port_config, str):
        raise ValueError("[install.endpoint] port_config must be a repo-relative JSON file")
    port_key = table.get("port_key", DEFAULT_PORT_KEY)
    if not isinstance(port_key, str) or not all(port_key.split(".")):
        raise ValueError('[install.endpoint] port_key must be a dotted key such as "port" or "webapp.port"')
    return Endpoint(scheme=scheme, port=_port(table.get("port", top_port)), version_path=path,
                    self_signed=self_signed, port_key=port_key,
                    port_config=_repo_path(port_config, "[install.endpoint] port_config") if port_config else "")


def local_port(root: Path, endpoint: Endpoint) -> tuple[int | None, str]:
    """The port this PC serves on and where it came from; (None, why) when the local config cannot tell.

    ``port_config`` wins when it is there and holds ``port_key``; an absent file or key falls back to the
    declared port, because the app then runs on its default. A file that does not parse, or a value that
    is not a port, is unknown: the app's real port cannot be told.
    """
    if not endpoint.port_config:
        return endpoint.port, "declared"
    name = endpoint.port_config
    try:
        value: Any = json.loads((root / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return endpoint.port, f"declared; {name} is not here"
    except (OSError, UnicodeDecodeError, ValueError):
        return None, f"{name} cannot be read or does not parse"
    for part in endpoint.port_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return endpoint.port, f"declared; {name} sets no {endpoint.port_key}"
        value = value[part]
    try:
        return _port(value), f"{name} {endpoint.port_key}"
    except ValueError:
        return None, f"{name} {endpoint.port_key} is not a TCP port"


def parse_migration(data: dict[str, Any]) -> Migration:
    """The migration declaration from a parsed ``.fleet.toml``; ValueError names the first bad key."""
    install = data.get("install", {})
    if not isinstance(install, dict):
        raise ValueError("[install] must be a table")
    table = install.get("migrate")
    if not isinstance(table, dict):
        raise ValueError("[install.migrate] is not declared")
    where = "[install.migrate]"
    mig = Migration(copy=_relative_list(table, "copy", where), secrets=_relative_list(table, "secrets", where))
    shared = table.get("shared", [])
    if not isinstance(shared, list) or not all(isinstance(n, str) and SHARED_NAME.fullmatch(n) for n in shared):
        raise ValueError(f"{where} shared must be a list of runtime-data folder names, not paths")
    mig.shared = list(shared)
    start = table.get("start", DEFAULT_START)
    if not isinstance(start, list) or not start or not all(isinstance(part, str) and part for part in start):
        raise ValueError(f"{where} start must be a non-empty list of strings")
    mig.start = list(start)
    mig.endpoint = parse_endpoint(install.get("endpoint"), data.get("port"))
    return mig


def read_fleet_toml(root: Path) -> dict[str, Any]:
    """``root/.fleet.toml`` parsed; ValueError when it is missing or does not parse."""
    try:
        import tomllib
    except ModuleNotFoundError as exc:
        raise ValueError("reading .fleet.toml needs Python 3.11 or newer") from exc
    try:
        return tomllib.loads((root / ".fleet.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f".fleet.toml cannot be read ({type(exc).__name__})") from exc
