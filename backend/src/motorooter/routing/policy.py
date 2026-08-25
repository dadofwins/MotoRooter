"""Intent -> provider resolution.

The table is configuration, not code, so changing which engine handles dirt is a config
edit. Requirements are validated eagerly at construction: a policy that would route
unpaved legs through a paved-only engine fails the deploy rather than the ride.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from motorooter.routing.errors import RoutingConfigError, UnsupportedIntent
from motorooter.routing.models import LegIntent
from motorooter.routing.protocol import RoutingProvider
from motorooter.routing.registry import ProviderRegistry


class IntentPolicy(BaseModel):
    """How one leg intent should be routed."""

    model_config = ConfigDict(frozen=True)

    provider: str
    requires_unpaved: bool = False
    """Assert the provider can weight toward dirt. Checked at startup."""

    requires_elevation: bool = False
    requires_map_matching: bool = False

    profile_params: dict[str, Any] = Field(default_factory=dict)
    """Opaque, provider-specific tuning passed through to the adapter."""

    def required_capabilities(self) -> dict[str, bool]:
        """Requirement flags translated to `ProviderCapabilities` field names."""
        required = {
            "prefers_unpaved": self.requires_unpaved,
            "elevation": self.requires_elevation,
            "map_matching": self.requires_map_matching,
        }
        return {field: True for field, needed in required.items() if needed}


class PolicyResolver:
    """Resolves a leg intent to the provider configured to handle it."""

    def __init__(
        self,
        registry: ProviderRegistry,
        table: Mapping[LegIntent, IntentPolicy],
    ) -> None:
        if not table:
            msg = "policy table is empty; no leg intent could be routed"
            raise RoutingConfigError(msg)
        self._registry = registry
        self._table = dict(table)
        self._validate()

    def _validate(self) -> None:
        """Fail fast on every misconfiguration the table can express."""
        for intent, policy in self._table.items():
            if policy.provider not in self._registry:
                available = ", ".join(sorted(self._registry.names())) or "<none>"
                msg = (
                    f"policy for {intent.value} names unregistered provider "
                    f"{policy.provider!r}; registered: {available}"
                )
                raise RoutingConfigError(msg)

            caps = self._registry.get(policy.provider).capabilities
            missing = [
                field for field in policy.required_capabilities() if not getattr(caps, field)
            ]
            if missing:
                msg = (
                    f"policy for {intent.value} requires {', '.join(missing)} but provider "
                    f"{policy.provider!r} does not support it"
                )
                raise RoutingConfigError(msg)

    def configured_intents(self) -> list[LegIntent]:
        """Intents this resolver can route. Surfaced to the frontend."""
        return list(self._table)

    def policy_for(self, intent: LegIntent) -> IntentPolicy:
        try:
            return self._table[intent]
        except KeyError:
            msg = f"no routing policy configured for intent {intent.value!r}"
            raise UnsupportedIntent(msg) from None

    def resolve(self, intent: LegIntent, override: str | None = None) -> RoutingProvider:
        """Provider for this intent.

        Args:
            intent: what the leg is for.
            override: pin this leg to a named provider, bypassing the table. Unlike the
                table, overrides are not capability-checked — an explicit user choice is
                honoured as given.
        """
        if override is not None:
            return self._registry.get(override)
        return self._registry.get(self.policy_for(intent).provider)

    def live_update_interval_ms(self, intent: LegIntent) -> int | None:
        """Drag-throttle budget for this intent's provider.

        `None` means preview-only: the UI rubber-bands during the gesture and routes on
        release. Surfaced to the frontend so throttling never hardcodes an engine name.
        """
        return self.resolve(intent).capabilities.live_update_interval_ms
