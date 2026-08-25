"""Cross-cutting concerns as providers that wrap providers.

Each decorator is itself a `RoutingProvider`, so they compose in any order and apply
uniformly to every engine. Recommended stack, outermost first:

    CachingProvider(QuotaGuardProvider(RetryingProvider(OrsProvider(...))))

Caching outside quota so cache hits cost no budget; retry innermost so a retried call is
correctly charged as the separate upstream request it is.
"""
