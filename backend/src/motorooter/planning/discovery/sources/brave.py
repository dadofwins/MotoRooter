"""Brave web search adapter.

This is the only stage that can find out a road is *good*. Places knows a restaurant exists
and what it is rated; it does not know that a pass is the reason people ride in that county,
or that a forest road washes out after spring melt. That lives in ride reports, forum threads
and BDR guides, and web search is the only way to reach it.

Which means the `description` field matters as much as the title. It is the judge's actual
evidence, and it is the one input to scoring that no metric will ever produce.

Over `httpx`, like every other adapter here, so it is testable with `respx`.

**Nothing here claims a coordinate.** Brave returns web pages; any location inferred from one
would be a guess wearing the clothes of a fact, and the resolve stage exists precisely to stop
that reaching the map.
"""

import html
import re
from typing import Any

import httpx

from motorooter.planning.discovery.errors import (
    DiscoveryQuotaExceeded,
    DiscoveryRateLimited,
    DiscoveryRefused,
    DiscoveryUnavailable,
)
from motorooter.planning.discovery.models import Candidate
from motorooter.planning.discovery.protocol import DEFAULT_RESULT_LIMIT
from motorooter.planning.discovery.queries import SearchQuery
from motorooter.routing.models import Coordinate

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

BRAVE_SOURCE = "brave"

_TAG = re.compile(r"<[^>]+>")
"""Brave marks matched terms with real HTML — `<strong>Snag Lake</strong>` — in both titles
and descriptions. Those strings are the judge's evidence and are shown to a rider, so the
markup is noise to a model and visible junk to a human. Found by running the spike."""


class BraveSearchSource:
    """Candidate places from Brave web search."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = BRAVE_SEARCH_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        """
        Args:
            api_key: Brave subscription token. Sent as a header, never a query parameter —
                a key in a URL leaks into logs, referrers and error messages.
            base_url: override for a recording proxy.
            client: injectable HTTP client, so callers can share a connection pool.
            timeout_s: per-request timeout.
        """
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._timeout_s = timeout_s

    @property
    def name(self) -> str:
        return BRAVE_SOURCE

    async def search(
        self,
        query: SearchQuery,
        *,
        near: Coordinate,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> tuple[Candidate, ...]:
        response = await self._get(
            {"q": query.text, "count": str(limit)},
        )
        self._raise_for_status(response)
        return self._parse(response, query, near, limit)

    async def _get(self, params: dict[str, str]) -> httpx.Response:
        headers = {
            "X-Subscription-Token": self._api_key,
            "Accept": "application/json",
        }
        try:
            if self._client is not None:
                return await self._client.get(
                    self._base_url, params=params, headers=headers, timeout=self._timeout_s
                )
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                return await client.get(self._base_url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            msg = f"Brave search request failed: {exc}"
            raise DiscoveryUnavailable(msg) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code

        if status == 429:
            msg = "Brave rate limit reached (HTTP 429)"
            raise DiscoveryRateLimited(msg)
        if status == 402:
            # Plan exhausted. Retrying spends nothing and gains nothing.
            msg = "Brave search plan exhausted (HTTP 402)"
            raise DiscoveryQuotaExceeded(msg)
        if status >= 500:
            msg = f"Brave returned HTTP {status}"
            raise DiscoveryUnavailable(msg)
        msg = f"Brave refused the request (HTTP {status})"
        raise DiscoveryRefused(msg)

    def _parse(
        self,
        response: httpx.Response,
        query: SearchQuery,
        near: Coordinate,
        limit: int,
    ) -> tuple[Candidate, ...]:
        try:
            body = response.json()
        except ValueError as exc:
            msg = "Brave returned a body that was not JSON"
            raise DiscoveryUnavailable(msg) from exc

        results = self._results_in(body)
        candidates: list[Candidate] = []
        for entry in results[:limit]:
            if not isinstance(entry, dict):
                continue
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                # A result with no title has nothing to resolve against. Skipped rather than
                # fatal: one odd entry should not lose the other four.
                continue
            candidates.append(
                Candidate(
                    name=_clean(title) or title.strip(),
                    category=query.category,
                    found_near=near,
                    source=self.name,
                    snippet=_text_or_none(entry.get("description")),
                    url=_text_or_none(entry.get("url")),
                )
            )
        return tuple(candidates)

    @staticmethod
    def _results_in(body: Any) -> list[Any]:  # noqa: ANN401 -- raw upstream JSON
        """Web results, or none.

        Brave omits `web` entirely when nothing matched, so an absent key is an ordinary
        empty result rather than a malformed body. Anything else unexpected is also treated
        as empty: a shape change should degrade to "found nothing", not take down a corridor
        run that has already spent requests on other categories.
        """
        if not isinstance(body, dict):
            return []
        web = body.get("web")
        if not isinstance(web, dict):
            return []
        results = web.get("results")
        return results if isinstance(results, list) else []


def _text_or_none(value: Any) -> str | None:  # noqa: ANN401 -- raw upstream JSON
    if not isinstance(value, str):
        return None
    return _clean(value) or None


def _clean(text: str) -> str:
    """Strip Brave's match markup and decode entities.

    Tags first, then entities: doing it the other way round would turn `&lt;strong&gt;` into
    a real tag and then remove it, which silently deletes text a page actually contained.
    """
    return html.unescape(_TAG.sub("", text)).strip()
