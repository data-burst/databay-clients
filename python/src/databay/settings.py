"""
The client's declared defaults: retries, waits, output, timeouts and where state lives.

Every default the SDK or the CLI uses is declared here once, and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OutputFormat = Literal["json", "table", "csv"]
OUTPUT_FORMATS: tuple[OutputFormat, ...] = ("json", "table", "csv")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """
    Retries of one request: GETs and writes sent with an ``Idempotency-Key`` only.

    A retried answer is a 408, 429, 502, 503 or 504, or a connect or read failure.
    """

    max_retries: int = 3
    #: Seconds before retry 1, 2, 3.
    backoff_s: tuple[float, ...] = (1.0, 2.0, 4.0)
    #: A ``Retry-After`` longer than this is cut to it.
    retry_after_cap_s: float = 30.0
    statuses: frozenset[int] = frozenset({408, 429, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    """How a wait polls its status operation."""

    first_interval_s: float = 2.0
    backoff: float = 1.5
    max_interval_s: float = 15.0
    #: Used when the wait declares no ``timeout_s`` and the caller passes no ``--timeout``.
    default_timeout_s: float = 600.0
    #: ``pipe wait --until data`` without ``--timeout``: one 15-minute freshness-probe cycle plus slack.
    data_timeout_s: float = 1200.0
    #: Past the limit, a tick's later requests still go out for this long, so the last tick, which
    #: starts at the limit after its pause, can finish when the server answers normally.
    tick_grace_s: float = 5.0
    #: Transient failures (5xx, 408, 429, network) in a row that end a wait with that failure.
    max_transient_in_a_row: int = 3


@dataclass(frozen=True, slots=True)
class ClientSettings:
    retry: RetryPolicy = RetryPolicy()
    wait: WaitPolicy = WaitPolicy()
    #: Connect timeout and read timeout of one request, in seconds.
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    #: How long a server's ``/api/auth/client-config`` answer is reused before it is read again.
    client_config_ttl_s: float = 3600.0
    #: An access token with this many seconds of validity left or fewer is renewed before it is sent.
    refresh_margin_s: float = 60.0
    #: How long ``databay login`` waits for the browser to come back to the loopback callback.
    browser_login_timeout_s: float = 300.0
    #: How often the update notice asks PyPI for the newest release, and how long that request may take.
    update_check_interval_s: float = 86400.0
    update_check_timeout_s: float = 2.0


SETTINGS = ClientSettings()

#: The profile ``databay login`` writes when no ``--profile`` is passed.
DEFAULT_PROFILE = "default"
#: Where ``databay login`` keeps tokens unless ``--token-store`` says otherwise.
DEFAULT_TOKEN_STORE = "keyring"  # noqa: S105 — the store's name, not a secret
#: Where ``databay skill install`` puts the skill unless ``--scope`` says otherwise: the user's home.
DEFAULT_SKILL_SCOPE = "user"

#: Environment variables the client reads. Each is an explicit input, documented in ``--help``.
ENV_TOKEN = "DATABAY_TOKEN"  # noqa: S105 — the variable's name, not a token
ENV_OUTPUT = "DATABAY_OUTPUT"
ENV_CONFIG_DIR = "DATABAY_CONFIG_DIR"
ENV_STATE_DIR = "DATABAY_STATE_DIR"
ENV_NO_COLOR = "NO_COLOR"
ENV_NO_UPDATE_CHECK = "DATABAY_NO_UPDATE_CHECK"
#: Set by CI systems; the update notice stays off under it.
ENV_CI = "CI"

OUTPUT_RULE = (
    "Output: a table when stdout is a terminal, JSON otherwise. --output json|table|csv, "
    f"or {ENV_OUTPUT}, picks one; --output wins over {ENV_OUTPUT}."
)
