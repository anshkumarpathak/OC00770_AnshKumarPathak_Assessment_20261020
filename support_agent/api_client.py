"""HTTP client for the enterprise APIs: timeouts, retries, schema validation.

Retry policy
  - Retried: timeouts, connection errors, 429 and 5xx (temporary problems).
  - Not retried: 4xx (our request is wrong or a business rule said no) and
    schema errors (the upstream contract is broken; retrying won't fix it).
  - Backoff: 0.2s, 0.4s, ... with jitter, at most MAX_ATTEMPTS attempts.
  - Changes (unlock, role assignment) carry an Idempotency-Key so a retry after
    a timeout can't apply the same change twice.
"""
import random
import time
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from support_agent import config

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    kind = "api_error"


class NotFound(ApiError):
    kind = "not_found"


class Rejected(ApiError):
    """The upstream system refused the request (4xx business rule)."""
    kind = "rejected"


class SchemaError(ApiError):
    kind = "schema_error"


class UpstreamUnavailable(ApiError):
    kind = "unavailable"


@dataclass
class CallStats:
    service: str
    method: str
    path: str
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def retries(self) -> int:
        return max(self.attempts - 1, 0)


class ApiClient:
    def __init__(self, base_url: str, service: str, *, timeout: float = config.API_TIMEOUT_S,
                 max_attempts: int = config.MAX_ATTEMPTS, backoff: float = config.BACKOFF_BASE_S):
        self.service = service
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.calls: list[CallStats] = []   # read by the tracer after each tool call

    def request(self, method: str, path: str, schema: type[BaseModel], *, many: bool = False,
                json: dict | None = None, params: dict | None = None, idempotency_key: str | None = None):
        stats = CallStats(self.service, method, path)
        self.calls.append(stats)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        adapter = TypeAdapter(list[schema]) if many else TypeAdapter(schema)

        for attempt in range(1, self.max_attempts + 1):
            stats.attempts = attempt
            try:
                resp = self.http.request(method, path, json=json, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                stats.errors.append(type(exc).__name__)
                self._wait_or_give_up(attempt, f"{self.service} did not respond in time")
                continue

            if resp.status_code in RETRYABLE_STATUS:
                stats.errors.append(f"HTTP {resp.status_code}")
                self._wait_or_give_up(attempt, f"{self.service} returned HTTP {resp.status_code}")
                continue
            if resp.status_code == 404:
                raise NotFound(_detail(resp))
            if resp.status_code >= 400:
                raise Rejected(_detail(resp))

            try:
                return adapter.validate_python(resp.json())
            except (ValidationError, ValueError) as exc:
                stats.errors.append("schema_error")
                raise SchemaError(f"{self.service} sent data that failed validation: {_short(exc)}") from exc

        raise UpstreamUnavailable(f"{self.service} unavailable")  # pragma: no cover

    def _wait_or_give_up(self, attempt: int, message: str) -> None:
        if attempt >= self.max_attempts:
            raise UpstreamUnavailable(f"{message} after {attempt} attempts")
        time.sleep(self.backoff * 2 ** (attempt - 1) * (0.5 + random.random()))


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except ValueError:
        return resp.text


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()
        fields = ", ".join(".".join(str(p) for p in e["loc"]) or "body" for e in errs[:3])
        return f"{len(errs)} problem(s) in {fields}"
    return str(exc)[:120]


def service_desk_client() -> ApiClient:
    return ApiClient(config.SERVICE_DESK_URL, "service_desk")


def sap_client() -> ApiClient:
    return ApiClient(config.SAP_URL, "sap")
