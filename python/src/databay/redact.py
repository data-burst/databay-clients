"""What ``--verbose`` may print of a request: secret headers and secret body fields are replaced."""

from __future__ import annotations

from typing import Any

REDACTED = "<redacted>"
#: Lower case: header names are case-insensitive.
SECRET_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})


def headers(values: dict[str, str]) -> dict[str, str]:
    return {name: REDACTED if name.lower() in SECRET_HEADERS else value for name, value in values.items()}


def body(value: Any, secret_fields: frozenset[str]) -> Any:  # noqa: ANN401 — any JSON value
    """``value`` with every field named in ``secret_fields`` replaced, at any depth."""
    if isinstance(value, dict):
        return {k: REDACTED if k in secret_fields else body(v, secret_fields) for k, v in value.items()}
    if isinstance(value, list):
        return [body(item, secret_fields) for item in value]
    return value
