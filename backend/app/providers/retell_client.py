"""
Shared low-level HTTP client for every Retell*Provider. Centralizes auth,
base URL, timeout, and error mapping so individual providers stay thin.
"""
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings


class RetellAPIError(Exception):
    def __init__(self, status_code: int, message: str, body: dict | None = None):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Retell API error {status_code}: {message}")


class RetellClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.RETELL_API_KEY
        self.base_url = base_url or settings.RETELL_API_BASE_URL
        if not self.api_key:
            # Fail loudly and early rather than silently making
            # unauthenticated calls that will 401 downstream.
            raise RuntimeError(
                "RETELL_API_KEY is not configured. Set it in the environment before using RetellClient."
            )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=0.5, max=4),
        stop=stop_after_attempt(3),
    )
    def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=15.0) as client:
            resp = client.request(method, url, headers=self._headers(), json=json, params=params)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = None
            raise RetellAPIError(resp.status_code, resp.text, body)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()
