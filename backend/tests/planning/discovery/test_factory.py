"""Wiring, which is the part that fails silently.

Nothing here calls an API. The factory's whole job is to hand each stage the right client,
and a setting that is constructed but never attached looks exactly like one that works —
this branch already shipped one such parameter and caught it by hand rather than by test.

The budgets and efforts themselves are argued in `factory.py`; these assert they arrive.
"""

import pytest

from motorooter.llm.providers.openai import OpenAiClient
from motorooter.planning.discovery.factory import (
    EXTRACT_EFFORT,
    EXTRACT_TIMEOUT_S,
    JUDGE_TIMEOUT_S,
    DiscoverySettings,
    build_discovery,
    settings_from_env,
)
from motorooter.planning.discovery.retry import RetryingSearchSource
from motorooter.planning.discovery.sources.brave import BraveSearchSource


def configured() -> DiscoverySettings:
    return DiscoverySettings(
        brave_api_key="brave-test",
        openai_api_key="sk-test",
        places_api_key="places-test",
    )


class TestItBuildsOnlyWhenItCan:
    def test_no_keys_means_no_pipeline(self):
        """`None` rather than raising: discovery needs four keys, and a backend that refuses
        to boot without them takes every other endpoint down with it."""
        assert build_discovery(DiscoverySettings()) is None

    @pytest.mark.parametrize("missing", ["brave_api_key", "openai_api_key", "places_api_key"])
    def test_any_missing_key_means_no_pipeline(self, missing):
        settings = DiscoverySettings(**{**vars(configured()), missing: None})
        assert build_discovery(settings) is None

    def test_all_keys_means_a_pipeline(self):
        assert build_discovery(configured()) is not None

    def test_offline_disables_it_outright(self, monkeypatch):
        """There is no offline version of a stage that is nothing but external services."""
        monkeypatch.setenv("MOTOROOTER_OFFLINE", "1")
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("GOOGLE_MAPS_SERVER_KEY", "places-test")
        assert not settings_from_env().configured


class TestTheStagesGetTheClientTheyWereArguedFor:
    """The measured settings, asserted where they are actually attached."""

    @staticmethod
    def _client(stage) -> OpenAiClient:
        client = stage._client
        assert isinstance(client, OpenAiClient)
        return client

    def test_extraction_skips_the_thinking(self):
        """35-44s of reasoning for the same six places is the whole reason this knob exists."""
        pipeline = build_discovery(configured())
        assert pipeline is not None
        assert self._client(pipeline._extractor).reasoning_effort == EXTRACT_EFFORT

    def test_extraction_gets_the_short_budget(self):
        pipeline = build_discovery(configured())
        assert pipeline is not None
        assert self._client(pipeline._extractor)._timeout_s == EXTRACT_TIMEOUT_S

    def test_judging_keeps_the_default_effort(self):
        """Judging is the one thing here that is genuinely a judgement, and it is unmeasured
        — so it is left alone rather than tuned on the strength of extraction's result."""
        pipeline = build_discovery(configured())
        assert pipeline is not None
        assert self._client(pipeline._judge).reasoning_effort is None

    def test_judging_gets_the_long_budget(self):
        """Losing a judgement loses everything the run already paid for."""
        pipeline = build_discovery(configured())
        assert pipeline is not None
        assert self._client(pipeline._judge)._timeout_s == JUDGE_TIMEOUT_S

    def test_the_two_budgets_are_actually_different(self):
        """Guards the collapse this started as: one shared client for every stage."""
        assert EXTRACT_TIMEOUT_S < JUDGE_TIMEOUT_S


class TestSearchIsRetried:
    """The layer has to be reachable, which is the thing this project keeps getting wrong.

    Six things merged today were correct, tested, and called by nobody. A retry decorator
    that exists and is not wrapped around the source is the seventh, and it would look
    exactly like working code right up until the first 429.
    """

    def test_the_source_is_wrapped_in_retry(self):
        pipeline = build_discovery(configured())
        assert pipeline is not None
        assert isinstance(pipeline._source, RetryingSearchSource)

    def test_the_brave_source_is_still_underneath(self):
        """Wrapped, not replaced — candidates carry the source name as provenance."""
        pipeline = build_discovery(configured())
        assert pipeline is not None
        assert pipeline._source.name == BraveSearchSource(api_key="k").name
