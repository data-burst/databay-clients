"""
Base classes of the generated models.

A response model ignores fields it does not know, so a field a newer server adds never breaks
this client. A response enum is open: a value it does not know becomes an ``Unknown(<value>)``
member with ``is_unknown`` set, never one of the known members. Code that acts on an enum value
(a wait) treats an unknown one as an error; display shows it as it came.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class RequestModel(BaseModel):
    """A schema only requests use: a field it does not declare is an error, caught before sending."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OpenStrEnum(StrEnum):
    @classmethod
    def _missing_(cls, value: Any) -> OpenStrEnum | None:  # noqa: ANN401 — whatever the caller passed
        if not isinstance(value, str):
            return None
        member = str.__new__(cls, value)
        member._name_ = f"Unknown({value})"
        member._value_ = value
        return member

    @property
    def is_unknown(self) -> bool:
        return self._name_.startswith("Unknown(")
