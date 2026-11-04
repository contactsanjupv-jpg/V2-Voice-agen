"""In production the API schema and interactive docs must not be served (they enumerate every route); in development they are."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import docs_urls


def _client(is_production: bool) -> TestClient:
    return TestClient(FastAPI(**docs_urls(is_production)))


def test_production_serves_no_schema_or_docs():
    client = _client(True)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_development_still_serves_docs_and_schema():
    client = _client(False)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200, path
