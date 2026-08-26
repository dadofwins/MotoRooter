"""Every request field is read by a handler, or deliberately unread with a reason.

The mirror of `frontend/src/api/coverage.test.ts`, and it exists because that one structurally
cannot cover this. Theirs asserts every field is *read by app code*, which is the right test
for a response and meaningless for a request: a request field is written by the client and
read by us. So the same class of bug — a field in the contract that nothing consumes — hides
on this side of the boundary with nothing watching.

It found two immediately. `ReplanRequest.prompt` and `ReplanRequest.preserve_pinned` are in
the generated TypeScript, so a client can send a rider's "prefer hot springs" or ask to keep
their pinned camps, and the server discards both without a word. There is no way to discover
that short of reading our source.

**This cannot know whether a field *should* be read.** It insists the answer is written down,
which turns "nobody noticed" into "somebody decided" — and makes an expired reason visible,
which is how the frontend allowlist caught its own `ascent_m` entry going stale within an hour.
"""

import ast
import pathlib

import pytest

SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "motorooter"
SCHEMAS = SOURCE_ROOT / "api" / "schemas.py"

DELIBERATELY_UNREAD: dict[str, str] = {
    "ReplanRequest.prompt": (
        "A free-text steer is a real feature and not a quick wire-up: it is the one field "
        "where client-authored text would reach query generation, so where it enters the "
        "four-stage pipeline needs deciding before it is honoured. Proposed 2026-08-26, "
        "awaiting a decision. If the answer is no, remove it from the contract instead."
    ),
    "ReplanRequest.preserve_pinned": (
        "Possibly meaningless server-side: replan streams and never writes, so the merge "
        "happens on the client and preserving pinned POIs is already client-side. If that "
        "holds, the honest fix is removing it from the contract rather than implementing "
        "it, which needs integrator sign-off. Raised 2026-08-26, awaiting a decision."
    ),
}


def _attributes_read_by_handlers() -> set[str]:
    """Every attribute name accessed anywhere in the backend, except in the schema module.

    Parsed rather than grepped. A text search for `prompt` matches
    `from motorooter.chat.prompt import ...` and would report the field as read — a
    false negative in the dangerous direction, since it would quietly retire the tripwire
    for the one field it was built to catch.
    """
    accessed: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        if path == SCHEMAS:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute):
                accessed.add(node.attr)
    return accessed


def _request_fields() -> list[tuple[str, str]]:
    """`(Model, field)` for every declared field on every request model."""
    tree = ast.parse(SCHEMAS.read_text())
    return [
        (node.name, statement.target.id)
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.endswith("Request")
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    ]


FIELDS = _request_fields()


def test_there_are_request_models_to_check():
    """A tripwire that silently checks nothing is worse than none — this is the assertion
    that fails if the models are renamed out from under the discovery above."""
    assert len(FIELDS) > 5


@pytest.mark.parametrize(("model", "field"), FIELDS, ids=[f"{m}.{f}" for m, f in FIELDS])
def test_the_field_is_read_or_deliberately_not(model: str, field: str):
    key = f"{model}.{field}"
    if key in DELIBERATELY_UNREAD:
        pytest.skip(f"deliberately unread: {DELIBERATELY_UNREAD[key]}")
    assert field in _attributes_read_by_handlers(), (
        f"{key} is in the contract and no handler reads it. A client can send it and the "
        f"server will discard it without a word. Either read it, remove it from the "
        f"contract, or add it to DELIBERATELY_UNREAD with a reason."
    )


def test_the_allowlist_has_no_stale_entries():
    """An allowlist that outlives its reason is a lie with a citation.

    A field that has since been wired up should leave the list, or the next person reads a
    justification for behaviour that no longer exists.
    """
    read = _attributes_read_by_handlers()
    declared = {f"{model}.{field}" for model, field in FIELDS}
    for key in DELIBERATELY_UNREAD:
        model_field = key.split(".")[-1]
        assert key in declared, f"{key} is allowlisted but no longer exists on the model"
        assert model_field not in read, (
            f"{key} is allowlisted as unread but something reads it now. Remove the entry."
        )


def test_every_reason_is_a_reason():
    """A one-word entry is a shrug with a citation. The point of the list is the argument."""
    for key, reason in DELIBERATELY_UNREAD.items():
        assert len(reason.split()) >= 12, f"{key}: give an actual reason"
