"""
The ``X-Databay-Feature`` header on the 404 a feature gate answers.

A gated feature that is off answers ``404 {"detail": "Not Found"}``, and an authenticated caller also
gets this header naming the feature and why it is off. Standard library only: the server's tests load
this file to check that the header the gate sends parses here.
"""

from __future__ import annotations

import re
from typing import NamedTuple

FEATURE_HEADER = "x-databay-feature"

#: What the caller can do, per the documented ``source``. A source not listed here is not parsed.
FEATURE_SOURCE_HINTS: dict[str, str] = {
    "plan": "Your organization's plan does not include this feature. Ask whoever runs this server about changing it.",
    "override": "This feature was turned off for your organization. Ask whoever runs this server to turn it back on.",
    "ceiling": "This server has the feature turned off for everyone on it. Ask whoever runs the server to turn it on.",
    "prerequisite": "This feature needs another feature that is off. Ask whoever runs this server which one.",
    "not_ready": "This feature is still being set up on this server. Ask whoever runs it when it will be ready.",
    "refused": "This server turned the feature off because a check it runs did not pass. Ask whoever runs the server.",
    "legacy": "No plan covers your account. Check that it belongs to an organization, or ask whoever runs the server.",
}

_FEATURE_OFF = re.compile(
    rf"disabled; feature=(?P<feature>[a-z][a-z0-9_]*); source=(?P<source>{'|'.join(FEATURE_SOURCE_HINTS)})"
)


class FeatureOff(NamedTuple):
    feature: str
    source: str


def parse_feature_header(value: str | None) -> FeatureOff | None:
    """The feature a header names, or ``None`` when it is absent or not the documented shape."""
    match = _FEATURE_OFF.fullmatch(value) if value is not None else None
    return FeatureOff(match["feature"], match["source"]) if match is not None else None
