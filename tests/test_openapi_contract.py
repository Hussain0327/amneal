"""API contract freeze: committed OpenAPI snapshot + response_model coverage.

``regwatch/frontend/openapi.json`` is the committed contract snapshot the TS
codegen consumes (``npm run gen:types`` regenerates it together with
``lib/api-types.ts``). Two guards keep the freeze honest from the Python side
(CI's frontend-contract job re-checks the same files byte-for-byte):

  * the snapshot must equal the LIVE app schema - a route or model change
    without a regenerated snapshot fails HERE, in the local gate, in both
    drift directions (dict equality is symmetric);
  * every JSON-returning route must declare a response_model, so a new route
    cannot ship an unfrozen payload shape that the codegen never sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute
from pydantic import BaseModel

from regwatch.api.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO_ROOT / "regwatch" / "frontend" / "openapi.json"

REGEN_HINT = (
    "the committed OpenAPI snapshot is stale -- regenerate it (and the TS wire types) "
    "with `npm run gen:types` in regwatch/frontend/ and commit both files"
)

# Routes that legitimately carry no response_model: raw Response bodies (SSE
# stream, .docx bytes, Prometheus text) or bodyless 204s. Anything else that
# shows up without a model is a contract gap and must fail below.
NO_JSON_BODY_ROUTES = {
    ("POST", "/query/stream"),
    ("POST", "/whitepaper/runs/{run_id}/docx"),
    ("GET", "/metrics"),
    ("POST", "/auth/logout"),
    ("DELETE", "/sessions/{session_id}"),
}


def _api_routes(routes: Any) -> list[APIRoute]:
    """Every APIRoute reachable from ``routes``, recursing through included
    routers (FastAPI 0.137+ wraps them; see test_whitepaper_api.py)."""
    out: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            out.append(route)
        original = getattr(route, "original_router", None)
        sub = getattr(original, "routes", None)
        if sub is not None:
            out.extend(_api_routes(sub))
    return out


def test_openapi_snapshot_matches_live_schema() -> None:
    assert SNAPSHOT.exists(), f"missing {SNAPSHOT}; {REGEN_HINT}"
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    # Round-trip the live schema through JSON so both sides compare as plain
    # JSON data (tuples/enums normalized exactly as the export script emits).
    live = json.loads(json.dumps(app.openapi(), sort_keys=True))
    assert committed == live, REGEN_HINT


def test_every_json_route_declares_a_response_model() -> None:
    routes = _api_routes(app.routes)
    seen: set[tuple[str, str]] = set()
    missing: list[tuple[str, str]] = []
    for route in routes:
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            seen.add((method, route.path))
            if (method, route.path) in NO_JSON_BODY_ROUTES:
                continue
            # A concrete pydantic model only: FastAPI silently infers
            # response_model from a `-> dict[str, Any]` return annotation, and
            # that infers an UNFROZEN schema-less contract -- `is None` alone
            # would wave those through.
            model = route.response_model
            if model is None or not (isinstance(model, type) and issubclass(model, BaseModel)):
                missing.append((method, route.path))
    assert (
        not missing
    ), f"routes without a pydantic response_model (unfrozen wire contract): {sorted(missing)}"
    # Vacuous-success guards: the recursion must still surface the real
    # endpoints, and every exempted route must still exist (a renamed route
    # would otherwise leave a stale exemption masking a future gap).
    assert {("POST", "/query"), ("GET", "/watch/latest"), ("GET", "/health")} <= seen
    assert seen >= NO_JSON_BODY_ROUTES
