"""Tools the assistant can call, and the registry that publishes them.

A tool declares its arguments as a pydantic model, and the JSON Schema the model is shown is
generated from that same model. One declaration, so the schema the model is promised and the
shape the handler receives cannot drift — which they would, quickly, if the schema were
hand-written next to a handler that parsed a dict.

Every tool here must be a thin wrapper over the same service function the REST endpoint
calls. A tool that reimplements the endpoint's logic will agree with it today and diverge
later, and the divergence will be invisible because both paths return something plausible.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from motorooter.llm.errors import ToolCallFailed


class ToolArguments(BaseModel):
    """Base for a tool's argument model. Rejects fields it does not declare.

    Pydantic ignores unknown fields by default, which is the wrong default for model output:
    a model sending `radius` against a schema declaring `radius_m` gets the 5 km default
    silently. It asked for 50 km, got 5, and was told nothing — and neither was the user.

    Forbidding extras turns that into a reported error the model can correct, and publishes
    `additionalProperties: false` in the schema so it is told the rule up front rather than
    only after breaking it.
    """

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool produced.

    `content` is what the *model* reads — prose or compact JSON, whatever helps it decide
    what to do next. The remaining fields are what the *user interface* reads, so a long
    discovery run fills the map as it works rather than after. The two are separate because
    they want different things: the model needs enough to reason with, the map needs
    structured objects, and forcing one to serve both makes both worse.
    """

    content: str
    found: int = 0
    """How many items this tool produced, for progress reporting."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Structured result for the UI. Shape is the tool's own business."""


class Tool:
    """One capability the assistant can invoke.

    Subclass, set the three class attributes, and implement `run`. Argument parsing,
    validation, and schema generation are handled here so no tool re-implements them.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    """What the model reads to decide whether this is the right tool. It is a prompt, not a
    comment: vague descriptions are the most common cause of a model picking wrongly."""

    arguments: ClassVar[type[BaseModel]]
    """Must forbid extra fields — subclass `ToolArguments`. Checked at registration."""

    def spec(self) -> dict[str, Any]:
        """The tool declaration sent to the model."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.arguments.model_json_schema(),
        }

    def parse(self, raw: str) -> BaseModel:
        """Turn the model's argument string into a validated arguments object.

        Raises:
            ToolCallFailed: malformed JSON, or JSON of the wrong shape. Both are the
                model's mistakes and both are recoverable — the caller reports the message
                back and lets it try again — so the message is written to be read by a
                model, naming the field rather than describing the exception.
        """
        try:
            parsed = json.loads(raw or "{}")
        except ValueError as exc:
            msg = f"arguments for {self.name!r} are not valid JSON: {exc}"
            raise ToolCallFailed(msg) from exc
        except RecursionError as exc:
            # `json.loads` raises this, not a ValueError, on deeply nested input — so a
            # model sending 60,000 open brackets aborted the whole run mid-stream. It is a
            # malformed argument like any other and belongs back with the model.
            msg = f"arguments for {self.name!r} are nested too deeply to parse"
            raise ToolCallFailed(msg) from exc

        try:
            return self.arguments.model_validate(parsed)
        except ValidationError as exc:
            raise ToolCallFailed(self._explain(exc)) from exc

    def _explain(self, exc: ValidationError) -> str:
        """Describe a validation failure in terms a model can act on.

        Not `str(ValidationError)`: that embeds the offending input value and a docs URL,
        both of which then travel to the model and onward to an unauthenticated screen. The
        field path and the expected type are the parts that help it correct itself, and they
        are the parts we author.
        """
        problems = [
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
            for error in exc.errors()
        ]
        return f"arguments for {self.name!r} do not match its schema — {'; '.join(problems)}"

    async def run(self, arguments: Any) -> ToolOutcome:  # noqa: ANN401 -- narrowed by subclass
        raise NotImplementedError


class ToolRegistry:
    """The set of tools published to the model, by name."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if not _forbids_extra_fields(tool.arguments):
                # A startup check rather than a runtime surprise: a tool author who forgets
                # `ToolArguments` would otherwise ship a tool that silently drops fields.
                msg = (
                    f"tool {tool.name!r} has an arguments model that allows extra fields; "
                    "subclass ToolArguments so unknown fields are refused rather than ignored"
                )
                raise ToolCallFailed(msg)
            if tool.name in self._tools:
                # Same reasoning as the provider registry: registration order would quietly
                # decide which implementation the model reaches.
                msg = f"duplicate tool name {tool.name!r}"
                raise ToolCallFailed(msg)
            self._tools[tool.name] = tool

        if not self._tools:
            # An agent with no tools still answers, so this fails as a chat box rather than
            # as an error — which is a very confusing thing to debug from the outside.
            msg = "a tool registry with no tools would make the assistant unable to act"
            raise ToolCallFailed(msg)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def names(self) -> Sequence[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool:
        """Raises ToolCallFailed naming the alternatives, which is what a model can act on."""
        try:
            return self._tools[name]
        except KeyError:
            available = ", ".join(sorted(self._tools))
            msg = f"no tool named {name!r}. Available tools: {available}"
            raise ToolCallFailed(msg) from None


def _forbids_extra_fields(model: type[BaseModel]) -> bool:
    return model.model_config.get("extra") == "forbid"
