"""One openFDA api_key policy, honoured by every caller.

Three call sites used to carry their own copy of "attach api_key when it is
configured": `assemble.dossier._fetch_rld_label`, `watch.aliases`, and
`watch.watchlist`. They now route through
`regwatch.sources._utils.openfda_params`.

These tests assert the observable result -- the outgoing query string -- rather
than the call, so re-inlining a fourth copy or dropping the policy at any one
site fails here.

Two notes for whoever edits this next:

* Only the helper's settings read is patched. A site that stopped calling the
  helper and re-inlined its own `get_settings()` would bypass that patch, and is
  still caught only because `conftest._isolate_env` (autouse) forces
  OPENFDA_API_KEY empty, so the real value is falsy and the api_key="secret-key"
  case fails. Keep that fixture, or patch each site's own settings read too.
* Key ORDER in the query string is not asserted, deliberately. Routing aliases.py
  through the helper moved `skip` after `api_key`; openFDA is not order-sensitive
  and these read params by name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from regwatch.assemble import dossier
from regwatch.sources import _utils
from regwatch.watch import aliases as aliases_mod
from regwatch.watch import watchlist as watchlist_mod

# (setting value, whether api_key should appear in the outgoing params)
_API_KEY_CASES = [("secret-key", True), (None, False)]


def _pin_api_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Point ONLY the helper's settings read at `value`.

    Patching `_utils.get_settings` rather than the environment keeps each
    caller's own settings (timeouts, user agent, paths) real, and sidesteps
    pydantic-settings reading a host `.env` that `delenv` cannot neutralize.
    """
    monkeypatch.setattr(_utils, "get_settings", lambda: SimpleNamespace(openfda_api_key=value))


def _recorder(recorded: list[httpx.QueryParams]) -> httpx.MockTransport:
    """Build a transport recording each request's params, returning no results."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request.url.params)
        return httpx.Response(200, json={"results": []})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(("api_key", "expected"), _API_KEY_CASES)
def test_aliases_discovery_applies_api_key_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, api_key: str | None, expected: bool
) -> None:
    _pin_api_key(monkeypatch, api_key)
    recorded: list[httpx.QueryParams] = []
    with httpx.Client(transport=_recorder(recorded)) as client:
        aliases_mod.discover_applicant_aliases(
            "ACME", client=client, cache_path=tmp_path / "aliases.json", refresh=True
        )

    assert len(recorded) == 1
    params = recorded[0]
    assert ("api_key" in params) is expected
    if expected:
        assert params["api_key"] == "secret-key"
    # Pagination and query survived the move to the shared helper.
    assert params["search"] == "sponsor_name:ACME*"
    assert params["limit"] == "100"
    assert params["skip"] == "0"


@pytest.mark.parametrize(("api_key", "expected"), _API_KEY_CASES)
def test_watchlist_fetch_applies_api_key_policy(
    monkeypatch: pytest.MonkeyPatch, api_key: str | None, expected: bool
) -> None:
    _pin_api_key(monkeypatch, api_key)
    recorded: list[httpx.QueryParams] = []
    with httpx.Client(transport=_recorder(recorded)) as client:
        watchlist_mod.fetch_drugsfda_for_company(
            ["ACME"], client=client, page_limit=100, max_pages=1
        )

    assert len(recorded) == 1
    params = recorded[0]
    assert ("api_key" in params) is expected
    if expected:
        assert params["api_key"] == "secret-key"
    assert params["search"] == "sponsor_name:ACME"
    assert params["limit"] == "100"
    assert params["skip"] == "0"


@pytest.mark.parametrize(("api_key", "expected"), _API_KEY_CASES)
def test_dossier_rld_label_applies_api_key_policy(
    monkeypatch: pytest.MonkeyPatch, api_key: str | None, expected: bool
) -> None:
    _pin_api_key(monkeypatch, api_key)
    recorded: list[httpx.QueryParams] = []
    real_client = httpx.Client

    def _factory(**_kwargs: Any) -> httpx.Client:
        # Bind the real constructor first: dossier resolves Client on the httpx
        # module at call time, so httpx.Client() in here would re-enter this.
        return real_client(transport=_recorder(recorded))

    monkeypatch.setattr(httpx, "Client", _factory)
    dossier._fetch_rld_label("Albuterol Sulfate", "208574")

    assert len(recorded) == 1
    params = recorded[0]
    assert ("api_key" in params) is expected
    if expected:
        assert params["api_key"] == "secret-key"
    assert params["search"] == 'openfda.application_number:"NDA208574"'
    assert params["limit"] == "1"
