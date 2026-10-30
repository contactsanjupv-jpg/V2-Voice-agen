"""
A (method, path) registered twice means one handler silently never runs (the first registered wins), which is how a
copy-pasted block could one day shadow a stricter or newer implementation. The app must register each route once.
"""
from collections import Counter

from fastapi.routing import APIRoute

from app.main import app


def _walk(routes):
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _walk(nested.routes)
        else:
            yield route


def test_every_method_and_path_is_registered_exactly_once():
    seen = Counter()
    for route in _walk(app.routes):
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                seen[(method, route.path)] += 1
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    assert not duplicates, f"routes registered more than once: {duplicates}"


def test_browse_routes_are_registered_once_and_stay_organization_scoped():
    paths = [r.path for r in _walk(app.routes) if isinstance(r, APIRoute) and r.path.endswith("/browse")]
    assert sorted(paths) == [
        "/api/v1/orgs/{organization_id}/calls/browse",
        "/api/v1/orgs/{organization_id}/leads/browse",
    ]
