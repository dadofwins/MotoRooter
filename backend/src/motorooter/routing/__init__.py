"""Pluggable routing layer.

No provider name may appear outside its own adapter module. Callers resolve a provider
through the policy resolver and talk to it via the `RoutingProvider` protocol.
"""
