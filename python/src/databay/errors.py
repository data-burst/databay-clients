"""
The SDK's exceptions: one per error code, each with its exit code and recovery.

A server answer's ``code`` becomes the generated exception for that code (``databay.error_codes``),
a subclass of ``DatabayApiError``. Answers that do not fit the error contract, and failures the
client finds itself, are the named classes below; ``databay.error_catalog`` holds their codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from databay.error_catalog import LOCAL_CODES, server_exit


@dataclass(frozen=True, slots=True)
class Recovery:
    action: str
    side_effects: str
    max_retries: int | None


class DatabayError(Exception):
    """
    Every error the SDK raises. ``code`` is stable; ``exit_code`` is the CLI's exit for it.

    ``details`` holds the extra top-level fields of a server error body (``revision``, ``quota``, …),
    and, for a client error, the values that explain it (the version, the state).
    """

    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        hint: str | None = None,
        next_command: str | None = None,
        details: dict[str, Any] | None = None,
        attempts: int | None = None,
        support_ref: str | None = None,
        docs: str | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.status = status
        self._hint = hint
        self.next_command = next_command
        self.details = details or {}
        self.attempts = attempts
        self.support_ref = support_ref
        self.docs = docs
        #: An answer the server did give before this failure: a not-ready verdict's body, or the
        #: answer of an operation whose wait then failed.
        self.result: Any = None

    @property
    def exit_code(self) -> int:
        return LOCAL_CODES[self.code].exit_code

    @property
    def recovery(self) -> Recovery:
        local = LOCAL_CODES[self.code]
        return Recovery(local.action, local.side_effects, local.max_retries)

    @property
    def hint(self) -> str:
        return self._hint or LOCAL_CODES[self.code].hint

    def to_dict(self) -> dict[str, Any]:
        """The error object the CLI prints (``{"error": ...}``)."""
        recovery = self.recovery
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "next": self.next_command,
            "docs": self.docs,
            "support_ref": self.support_ref,
            "status": self.status,
            "attempts": self.attempts,
            "details": self.details,
            "recovery": {
                "action": recovery.action,
                "side_effects": recovery.side_effects,
                "max_retries": recovery.max_retries,
            },
        }
        return body


class DatabayApiError(DatabayError):
    """A refusal the server answered with a registered ``code``; the generated classes subclass it."""

    def _info(self) -> Any:  # noqa: ANN401 — an ErrorInfo from the generated registry
        from databay.error_codes import REGISTRY  # noqa: PLC0415 — loaded only on an error

        return REGISTRY[self.code]

    @property
    def exit_code(self) -> int:
        return server_exit(self.code, self._info().action)

    @property
    def recovery(self) -> Recovery:
        info = self._info()
        return Recovery(info.action, info.side_effects, info.max_retries)

    @property
    def hint(self) -> str:
        return self._hint or self._info().hint


def api_error(code: str, message: str, **kwargs: Any) -> DatabayApiError:  # noqa: ANN401
    """The generated exception for a registered server ``code``; a code missing from the registry is a ``KeyError``."""
    from databay.error_codes import EXCEPTIONS  # noqa: PLC0415 — loaded only on an error

    return EXCEPTIONS[code](message, **kwargs)


class DatabayUsageError(DatabayError):
    """The command line or the SDK call is incomplete or inconsistent (exit 2)."""

    code = "usage_invalid"


class DatabayProfileError(DatabayError):
    """No profile, a broken config file, or a token the profile names is not there (exit 3)."""

    code = "no_profile"


class DatabayCancelled(DatabayError):
    code = "cancelled"


class DatabayUncodedError(DatabayError):
    """A JSON error body without a ``code``: the server broke the error contract."""

    code = "uncoded_error"


class DatabayFeatureDisabled(DatabayError):
    """A gated route's feature is off for this caller; ``feature`` and ``source`` say which and why."""

    code = "feature_disabled"

    def __init__(self, message: str, *, feature: str, source: str, **kwargs: Any) -> None:  # noqa: ANN401
        from databay.feature_header import FEATURE_SOURCE_HINTS  # noqa: PLC0415 — loaded only on an error

        super().__init__(
            message, hint=FEATURE_SOURCE_HINTS[source], details={"feature": feature, "source": source}, **kwargs
        )
        self.feature = feature
        self.source = source


class DatabayUnknownCodeError(DatabayError):
    """A ``code`` this client version does not know: a newer server added it."""

    code = "unknown_error_code"


class DatabayServerUnavailable(DatabayError):
    """A non-JSON error body: a proxy or a maintenance page answered, not Databay."""

    code = "server_unavailable"


class DatabayNetworkError(DatabayError):
    code = "network_error"


class DatabayResponseInvalid(DatabayError):
    """A 2xx body that does not fit the generated model; it names the field, never the body."""

    code = "response_invalid"


class DatabayVersionError(DatabayError):
    code = "server_version_mismatch"


class DatabayWaitError(DatabayError):
    """
    A wait that did not succeed.

    Its code is ``wait_pending`` (timed out, resumable), ``wait_failed``, ``unexpected_wait_state``,
    ``wait_target_gone`` or ``wait_feature_disabled``. ``outcome`` is the wait's last state, which the
    CLI prints on stdout.
    """

    code = "wait_failed"

    def __init__(self, message: str, *, outcome: Any = None, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(message, **kwargs)
        self.outcome = outcome


class DatabayNotReady(DatabayError):
    """A verdict route answered a not-ready verdict; ``result`` is the whole answer."""

    code = "not_ready"

    def __init__(self, message: str, *, result: Any = None, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(message, **kwargs)
        self.result = result


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """One server code's registry entry (``contract/error_codes.json``)."""

    hint: str
    action: str
    side_effects: str
    max_retries: int | None
