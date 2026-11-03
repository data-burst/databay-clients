"""
What one exposed API operation is to the SDK and CLI, as plain data.

The generated ``databay.operations.<noun>`` modules hold one ``Operation`` per route, built from the
route's ``x-databay-client`` declaration (docs/client-contract.md). Both the SDK resources and the
CLI commands read them, so the two never disagree on a path, a flag or a wait. This module imports
only the standard library: ``python/tools/gen_client.py`` builds these objects too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Location = Literal["path", "query", "header", "body"]
#: How a value is read from the command line: ``json`` takes a JSON document (objects and arrays
#: of objects), ``list`` repeats the flag once per item.
ValueKind = Literal["string", "integer", "number", "boolean", "json", "list"]
ResponseKind = Literal["json", "text", "empty"]


@dataclass(frozen=True, slots=True)
class Param:
    """One input of an operation: a path, query or header parameter, or a body field."""

    name: str
    location: Location
    kind: ValueKind
    required: bool
    description: str = ""
    #: Marked ``format: password`` in the spec: never a value flag, redacted in verbose logs.
    secret: bool = False
    #: The values an enum takes; for a ``list`` of an enum, the values of one item.
    choices: tuple[str, ...] = ()
    #: The kind of one item of a ``list``.
    item_kind: ValueKind | None = None
    #: The schema allows ``null``: the SDK may send ``None`` for it.
    nullable: bool = False

    @property
    def flag(self) -> str:
        """The CLI flag without dashes: the name in kebab case."""
        return self.name.replace("_", "-").lower()

    @property
    def identifier(self) -> str:
        """The Python identifier the SDK and the CLI callback use for it."""
        return python_identifier(self.name)


@dataclass(frozen=True, slots=True)
class Body:
    """
    An operation's JSON body.

    A plain body has ``fields``. A discriminated body has one variant per value of
    ``discriminator``, each with its own fields; the CLI makes each variant a subcommand. With
    ``variant_field`` set, the variants are that one property of a plain body: a variant's fields
    and the discriminator nest under it on the wire, and ``fields`` are the properties beside it.
    """

    schema: str | None
    fields: tuple[Param, ...] = ()
    discriminator: str | None = None
    variants: tuple[tuple[str, str, tuple[Param, ...]], ...] = ()
    variant_field: str | None = None

    def variant(self, key: str) -> tuple[str, tuple[Param, ...]]:
        for variant_key, schema, fields in self.variants:
            if variant_key == key:
                return schema, fields
        msg = f"{self.schema} has no variant {key!r}"
        raise KeyError(msg)

    def fields_of(self, variant: str | None) -> tuple[Param, ...]:
        """Every field a command of ``variant`` takes as a flag: the body's own, then the variant's."""
        return self.fields if variant is None else (*self.fields, *self.variant(variant)[1])


@dataclass(frozen=True, slots=True)
class Condition:
    """How a status value that needs a second field is classified, by that field's value."""

    field: str
    pending: tuple[str, ...] = ()
    success: tuple[str, ...] = ()
    failure: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Wait:
    """
    How the client waits on what an operation started, by polling an exposed status operation.

    ``bind`` maps each status-operation parameter to a JSON pointer into this operation's response,
    or to ``path:<name>``, one of this operation's own path parameters.
    """

    status_operation: str
    bind: tuple[tuple[str, str], ...]
    field: str
    pending: tuple[str, ...]
    success: tuple[str, ...]
    failure: tuple[str, ...]
    error_field: str | None = None
    conditional: tuple[tuple[str, Condition], ...] = ()
    timeout_s: float | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """A 2xx body field whose ``not_ready`` values make the operation's answer a refusal."""

    field: str
    ok: tuple[str, ...]
    not_ready: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Response:
    kind: ResponseKind
    #: ``module:Class`` under ``databay.models`` for a JSON body, or ``None`` when it has no schema.
    model: str | None = None
    #: The top-level JSON type the schema allows: ``object``, ``array`` or ``any``.
    shape: str = "any"
    #: For an object answer that holds the operation's list (``{"keys": [...]}``): the JSON pointer
    #: of that list, whose items the declared ``columns`` name.
    rows: str | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    noun: str
    verb: str
    method: str
    path: str
    summary: str
    description: str
    project: str | None
    destructive: bool
    idempotent: bool
    since: str
    params: tuple[Param, ...]
    body: Body | None
    response: Response
    examples: tuple[str, ...]
    columns: tuple[str, ...] = ()
    wait: Wait | None = None
    verdict: Verdict | None = None
    #: Set on a status operation that a ``wait`` polls: the wait it can resume (``--wait`` on it).
    resumes: Wait | None = None

    @property
    def secret_fields(self) -> frozenset[str]:
        """Names of every secret body field, across all variants: what verbose logs redact."""
        if self.body is None:
            return frozenset()
        fields = list(self.body.fields) + [p for _, _, variant in self.body.variants for p in variant]
        return frozenset(p.name for p in fields if p.secret)

    @property
    def project_location(self) -> tuple[str, str] | None:
        """``(where, name)`` for ``path:``/``query:``/``body:`` locations; ``("header", "")``; ``None``."""
        if self.project is None:
            return None
        where, _, name = self.project.partition(":")
        return where, name

    @property
    def offers_wait(self) -> bool:
        """Whether the call can wait (``--wait``). A create answers at once; its ``wait`` only names ``next``."""
        return self.resumes is not None or (self.wait is not None and self.verb != "create")

    @property
    def path_params(self) -> tuple[Param, ...]:
        return tuple(p for p in self.params if p.location == "path")

    @property
    def command(self) -> str:
        return f"databay {self.noun} {self.verb}"


def python_identifier(name: str) -> str:
    """A wire name as a Python identifier: lower case, ``-`` and spaces as ``_``, a keyword suffixed ``_``."""
    import keyword  # noqa: PLC0415 — only the generator and the first call pay for it

    ident = "".join(c if c.isalnum() else "_" for c in name.lower())
    if not ident or ident[0].isdigit():
        ident = f"_{ident}"
    return f"{ident}_" if keyword.iskeyword(ident) else ident
