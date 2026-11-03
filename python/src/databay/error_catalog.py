"""
Exit codes, and the errors the client itself raises.

Every error the CLI ends with has one code, and every code one exit code (the table below). Server
codes come from the generated ``databay.error_codes`` registry; their exit is ``server_exit``. The
client's own codes are ``LOCAL_CODES``. ``errors.md`` is rendered from both. Standard library only:
``python/tools/gen_client.py`` reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["retry", "fix_and_retry", "do_first", "ask_human"]
SideEffects = Literal["none", "partial", "complete", "unknown"]

#: Exit code -> what it means and what to do next: one next action per exit code.
EXIT_CODES: dict[int, str] = {
    0: "ok",
    1: "the server refused; fix the input or ask a human",
    2: "usage: the command line is incomplete or inconsistent",
    3: "a profile or a token is needed",
    4: "still pending: run `next`, the same wait",
    5: "the wait ended in failure",
    6: "transient: retry later",
    7: "version mismatch: upgrade the side the message names",
    8: "the client is outdated or hit a bug: upgrade it or report it",
    9: "not ready: fix what the answer names, then check again",
}


@dataclass(frozen=True, slots=True)
class LocalCode:
    exit_code: int
    action: Action
    side_effects: SideEffects
    hint: str
    #: How many times an agent may retry the same call before asking a human.
    max_retries: int | None = None
    #: The code is also a server code: its registry entry gives the hint, and this exit applies to both.
    server_code: bool = False


LOCAL_CODES: dict[str, LocalCode] = {
    "usage_invalid": LocalCode(2, "fix_and_retry", "none", "Read the command's --help and pass the flags it names."),
    "project_conflict": LocalCode(2, "fix_and_retry", "none", "", server_code=True),
    "no_active_project": LocalCode(
        2, "fix_and_retry", "none", "Pass --project <project_id>, or set active_project in the profile."
    ),
    "secret_required": LocalCode(
        2, "fix_and_retry", "none", "Pass the secret with --<field>-env NAME or --<field>-stdin; it is never a flag."
    ),
    "confirmation_required": LocalCode(
        2, "ask_human", "none", "This command removes or overwrites state. Pass --yes once a human has agreed."
    ),
    "unsupported_platform": LocalCode(2, "ask_human", "none", "Run databay on Linux or macOS; on Windows, use WSL."),
    "skill_exists": LocalCode(
        2,
        "ask_human",
        "none",
        "A skill from another CLI version is installed there. Pass --force to replace it with this version's.",
    ),
    "skill_write_failed": LocalCode(
        2,
        "ask_human",
        "none",
        "Nothing changed. Make the folder the message names writable, or install with the other --scope.",
    ),
    "unsupported_target": LocalCode(
        2,
        "fix_and_retry",
        "none",
        "Pass --target claude or --target codex; no other agent has a verified skill format.",
    ),
    "cancelled": LocalCode(1, "ask_human", "none", "Nothing was changed. Run the command again and answer y."),
    "feature_disabled": LocalCode(
        1,
        "ask_human",
        "none",
        "This feature is off for you or on this server; `details` names it and why. Ask whoever runs the server.",
    ),
    "stopped": LocalCode(
        1, "retry", "none", "Stopped while only reading; nothing changed. Run it again, or run `next` to resume a wait."
    ),
    "interrupted": LocalCode(
        1,
        "ask_human",
        "unknown",
        "Stopped after the request was sent: check its result before running it again. "
        "A create is safe to repeat only with the --idempotency-key it printed.",
    ),
    "no_profile": LocalCode(3, "do_first", "none", "Sign in with databay login --server URL, or pass --profile NAME."),
    "profile_invalid": LocalCode(3, "fix_and_retry", "none", "Fix config.toml as the message says."),
    "token_source_conflict": LocalCode(
        2,
        "fix_and_retry",
        "none",
        "Unset DATABAY_TOKEN, or pass --profile with a profile whose auth is env; the CLI never picks one for you.",
    ),
    "login_required": LocalCode(
        3, "do_first", "none", "Sign in again with the command `next` names, then run this command again."
    ),
    "credential_missing": LocalCode(
        3, "do_first", "none", "The profile has no stored sign-in. Run the command `next` names."
    ),
    "keyring_unavailable": LocalCode(
        3,
        "fix_and_retry",
        "none",
        "No usable OS keyring. Pass --token-store file to keep tokens in a 0600 file under the config dir.",
    ),
    "token_file_insecure": LocalCode(
        3, "fix_and_retry", "none", "Run chmod 600 on the file the message names; it holds tokens."
    ),
    "offline_access_not_granted": LocalCode(
        3,
        "ask_human",
        "none",
        "Keycloak did not grant offline_access: an admin gives the user that role "
        "(default-roles-databay composes it; see docs/cli-auth.md).",
    ),
    "expired_token": LocalCode(
        3, "retry", "none", "The sign-in code expired before it was approved. Run databay login again.", 1
    ),
    "access_denied": LocalCode(
        3, "ask_human", "none", "The sign-in was refused in the browser. Run databay login again to retry."
    ),
    "login_timeout": LocalCode(
        3, "retry", "none", "The browser did not come back in time. Run databay login again, or pass --device.", 1
    ),
    "login_failed": LocalCode(
        3, "ask_human", "none", "Keycloak refused the sign-in; the message names its OAuth error."
    ),
    "revoke_failed": LocalCode(
        1,
        "ask_human",
        "partial",
        "The local credential is deleted, but Keycloak may still hold the session: an admin revokes it "
        "(docs/cli-auth.md, Listing and revoking a user's CLI sessions).",
    ),
    "token_missing": LocalCode(
        3, "do_first", "none", "Set DATABAY_TOKEN to an access token; the profile reads it from there."
    ),
    "wait_pending": LocalCode(
        4, "retry", "complete", "Run `next`: it resumes the same wait and starts nothing again.", 3
    ),
    "wait_failed": LocalCode(
        5, "ask_human", "complete", "The operation ran and ended in a failure state; read the error it reports."
    ),
    "wait_feature_disabled": LocalCode(
        5,
        "ask_human",
        "unknown",
        "What the wait polled is behind a feature that is off, so it cannot tell how the operation ended. "
        "Ask whoever runs the server, then check the result before running a write again.",
    ),
    "unexpected_wait_state": LocalCode(
        5, "ask_human", "complete", "The server reported a state this CLI does not know. Upgrade the CLI."
    ),
    "wait_target_gone": LocalCode(
        5, "ask_human", "unknown", "What the wait polled no longer exists; it was deleted while waiting."
    ),
    "non_databay_answer": LocalCode(
        1,
        "fix_and_retry",
        "none",
        "Something other than Databay answered (a proxy, a gateway or a wrong server URL): check the profile's server.",
    ),
    "network_error": LocalCode(6, "retry", "unknown", "Check the server URL and the network, then run it again.", 3),
    "server_unavailable": LocalCode(
        6, "retry", "unknown", "The server answered without a Databay error body. Try again later.", 3
    ),
    "server_version_mismatch": LocalCode(
        7, "ask_human", "none", "The CLI and the server speak different API majors. Upgrade the older side."
    ),
    "server_too_old": LocalCode(
        7, "ask_human", "none", "This command needs a newer server. Upgrade the server or use an older CLI."
    ),
    "unsupported_product": LocalCode(
        7, "ask_human", "none", "This server runs Databay for Shopify, which the CLI does not support."
    ),
    "unknown_error_code": LocalCode(
        8, "ask_human", "unknown", "The server answered a code this CLI does not know. Upgrade the CLI."
    ),
    "response_invalid": LocalCode(
        8, "ask_human", "unknown", "The answer does not match this CLI's models. Upgrade the CLI or report it."
    ),
    "uncoded_error": LocalCode(
        8, "ask_human", "unknown", "The server answered an error without a code. Report it with the request id."
    ),
    "internal_error": LocalCode(
        8, "ask_human", "unknown", "The CLI failed unexpectedly. Report it with the crash log it names."
    ),
    "not_ready": LocalCode(9, "fix_and_retry", "none", "Fix what the answer names, then run the check again."),
}

#: The hint of ``uncoded_error`` on a 404: the feature gate sends no header to a caller it could not identify.
UNCODED_404_HINT = (
    "A 404 without a code can be a route this server does not have, a sign-in it no longer accepts, or a feature "
    "that is off where the server does not say so. Sign in again, then report it with the request id."
)

#: Server codes whose exit is not the default: the caller must sign in again, or fix the command line.
SERVER_EXIT_OVERRIDES: dict[str, int] = {"token_invalid": 3, "token_stale": 3, "project_conflict": 2}


def server_exit(code: str, action: Action) -> int:
    """The exit code of a server error code: an override, else 6 when the registry says retry, else 1."""
    if code in SERVER_EXIT_OVERRIDES:
        return SERVER_EXIT_OVERRIDES[code]
    return 6 if action == "retry" else 1
