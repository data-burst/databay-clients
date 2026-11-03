"""
Whether this client may call an operation on a server, from the server's ``/api/auth/client-config``.

The answer is cached per server in the state dir for ``client_config_ttl_s``. A server running
another product, another API major, or an older minor than the operation's ``since`` is refused
before the operation is sent.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from databay.errors import DatabayResponseInvalid, DatabayVersionError
from databay.paths import state_dir
from databay.spec import API_VERSION
from databay.transport import Request, error_from

if TYPE_CHECKING:
    from pathlib import Path

    from databay.operation import Operation
    from databay.transport import Transport

CLIENT_CONFIG_PATH = "/api/auth/client-config"
SUPPORTED_PRODUCT = "core"


@dataclass(frozen=True, slots=True)
class ServerInfo:
    product: str
    api_version: str
    issuer: str
    client_id: str


def _version(value: str) -> tuple[int, int]:
    major, _, minor = value.partition(".")
    return int(major), int(minor)


def _parse(body: Any) -> ServerInfo:  # noqa: ANN401 — any JSON value
    fields = ("product", "api_version", "issuer", "client_id")
    if not isinstance(body, dict) or not all(isinstance(body.get(f), str) for f in fields):
        missing = [f for f in fields if not isinstance(body, dict) or not isinstance(body.get(f), str)]
        raise DatabayResponseInvalid(f"GET {CLIENT_CONFIG_PATH}: the answer has no string {missing}")
    try:
        _version(body["api_version"])
    except ValueError as exc:
        raise DatabayResponseInvalid(f"GET {CLIENT_CONFIG_PATH}: api_version is not major.minor") from exc
    return ServerInfo(**{f: body[f] for f in fields})


def _cache_file(server: str) -> Path:
    return state_dir() / "client-config" / f"{hashlib.sha256(server.encode()).hexdigest()[:24]}.json"


def _cached(transport: Transport) -> ServerInfo | None:
    path = _cache_file(transport.server)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(entry["fetched_at"]) > transport.settings.client_config_ttl_s:
            return None
        return _parse(entry["body"])
    except (ValueError, KeyError, TypeError, DatabayResponseInvalid) as exc:
        transport.events("cache_invalid", {"file": str(path), "error": type(exc).__name__})
        return None


def _store(transport: Transport, body: Any) -> None:  # noqa: ANN401
    path = _cache_file(transport.server)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "body": body}), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # The cache only saves a request; a read-only state dir still lets the command run.
        transport.events("cache_write_failed", {"file": str(path), "error": type(exc).__name__})


def server_info(transport: Transport, *, fresh: bool = False) -> tuple[ServerInfo, bool]:
    """The server's client-config and whether it came from the cache; ``fresh`` reads the server."""
    cached = None if fresh else _cached(transport)
    if cached is not None:
        return cached, True
    request = Request("GET", CLIENT_CONFIG_PATH)
    answer = transport.exchange(request)
    if answer.status == 404 and answer.is_json:  # noqa: PLR2004
        # A Databay server without client-config predates the API; a non-JSON 404 is not Databay at all.
        raise DatabayVersionError(
            f"the server has no {CLIENT_CONFIG_PATH}: it predates API {API_VERSION}; upgrade the server",
            code="server_too_old",
            details={"client_api_version": API_VERSION},
        )
    if answer.status >= 400:  # noqa: PLR2004
        raise error_from(answer, request)
    try:
        body = answer.json()
    except ValueError as exc:
        raise DatabayResponseInvalid(f"GET {CLIENT_CONFIG_PATH}: the answer is not JSON") from exc
    info = _parse(body)
    _store(transport, body)
    return info, False


def ensure_compatible(
    transport: Transport, known: tuple[ServerInfo, bool] | None, op: Operation
) -> tuple[ServerInfo, bool]:
    """
    Check ``op`` against the server; ``known`` is an earlier ``(info, from_cache)``, else client-config is read.

    A refusal based on a cached answer reads the server once more first: it may have been upgraded.
    Returns the ``(info, from_cache)`` to keep for the next operation.
    """
    info, from_cache = known if known is not None else server_info(transport)
    try:
        check(info, op)
    except DatabayVersionError:
        if not from_cache:
            raise
        info, from_cache = server_info(transport, fresh=True)
        check(info, op)
    return info, from_cache


def check(info: ServerInfo, op: Operation) -> None:
    """Refuse a server this client cannot call ``op`` on: product, API major, then the operation's ``since``."""
    check_server(info)
    server_major, server_minor = _version(info.api_version)
    if _version(op.since) > (server_major, server_minor):
        raise DatabayVersionError(
            f"`{op.command}` needs server API {op.since}; the server is on {info.api_version}",
            code="server_too_old",
            details={"server_api_version": info.api_version, "client_api_version": API_VERSION, "since": op.since},
        )


def check_server(info: ServerInfo) -> None:
    """Refuse a server this client cannot talk to at all: another product, or another API major."""
    if info.product != SUPPORTED_PRODUCT:
        raise DatabayVersionError(
            f"this server runs the {info.product!r} product; the CLI supports {SUPPORTED_PRODUCT!r} only",
            code="unsupported_product",
            details={"product": info.product},
        )
    server_major, _ = _version(info.api_version)
    client_major, _ = _version(API_VERSION)
    versions = {"server_api_version": info.api_version, "client_api_version": API_VERSION}
    if server_major != client_major:
        side = "the CLI" if server_major > client_major else "the server"
        raise DatabayVersionError(
            f"the server speaks API {info.api_version} and this CLI speaks {API_VERSION}; upgrade {side}",
            details=versions,
        )
