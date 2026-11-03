"""
Commands written by hand, for work no single operation does: local state, sign-in, curated flows.

Each entry is ``command -> (module, attribute, summary)``. A one-word command sits beside the
generated nouns; ``"<noun> <verb>"`` adds a verb to that generated noun (``project use``). The root
group imports the module only when the command, or its noun, runs. An operation declared
``agent_shape="curated:<command>"`` gets no generated command; its command belongs here.
"""

HANDWRITTEN: dict[str, tuple[str, str, str]] = {
    "login": ("databay.cli.auth", "login", "Sign in and save a profile"),
    "logout": ("databay.cli.auth", "logout", "Sign out: revoke and delete the profile's credential"),
    "profile": ("databay.cli.profile", "profile", "Switch between and list profiles"),
    "status": ("databay.cli.profile", "status", "Who and where: profile, server, user, org, project, skill"),
    "skill": ("databay.cli.skill", "skill", "Install the agent skill for Claude Code or Codex"),
    "project use": ("databay.cli.profile", "project_use", "Set the profile's active project"),
    "pipe wait": ("databay.cli.pipe_wait", "pipe_wait", "Wait until a pipe is active or its table is ready to query"),
    "sql": ("databay.cli.sql", "sql", "Run one read-only SQL statement"),
}
