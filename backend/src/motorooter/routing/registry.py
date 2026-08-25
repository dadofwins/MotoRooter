"""Provider registry.

Holds the live provider instances and supports lookup by name *and* by capability. The
capability lookup is what lets dispatch and fallback logic avoid hardcoding engine names.
"""

from collections.abc import Iterable, Iterator

from motorooter.routing.errors import ProviderNotFound, RoutingConfigError
from motorooter.routing.models import ProviderCapabilities
from motorooter.routing.protocol import RoutingProvider

_CAPABILITY_FIELDS = frozenset(ProviderCapabilities.model_fields)


class ProviderRegistry:
    """Name -> provider, keyed on each provider's declared capability name."""

    def __init__(self, providers: Iterable[RoutingProvider]) -> None:
        self._providers: dict[str, RoutingProvider] = {}
        for provider in providers:
            name = provider.capabilities.name
            if name in self._providers:
                msg = (
                    f"duplicate provider name {name!r}; registration order would decide the winner"
                )
                raise RoutingConfigError(msg)
            self._providers[name] = provider

    def get(self, name: str) -> RoutingProvider:
        try:
            return self._providers[name]
        except KeyError:
            available = ", ".join(sorted(self._providers)) or "<none>"
            msg = f"unknown provider {name!r}; registered: {available}"
            raise ProviderNotFound(msg) from None

    def find(self, **requirements: object) -> list[RoutingProvider]:
        """Providers whose capabilities match every requirement (conjunctive).

        Requirement keys are `ProviderCapabilities` field names; an unrecognized key is a
        config error rather than a silently empty result.
        """
        unknown = set(requirements) - _CAPABILITY_FIELDS
        if unknown:
            msg = f"unknown capability field(s): {', '.join(sorted(unknown))}"
            raise RoutingConfigError(msg)
        return [
            provider
            for provider in self._providers.values()
            if all(
                getattr(provider.capabilities, field) == value
                for field, value in requirements.items()
            )
        ]

    def names(self) -> list[str]:
        return list(self._providers)

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def __iter__(self) -> Iterator[RoutingProvider]:
        return iter(self._providers.values())

    def __len__(self) -> int:
        return len(self._providers)
