from __future__ import annotations

import os
import re
from typing import Any

from .auth import get_session

API_VERSION = "v25"
BASE = f"https://googleads.googleapis.com/{API_VERSION}"
TIMEOUT = 120


class GoogleAdsApiError(RuntimeError):
    pass


def digits(value: str | int) -> str:
    return re.sub(r"\D", "", str(value))


def _headers(login_customer_id: str | int | None = None) -> dict[str, str]:
    token = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
    if not token:
        raise GoogleAdsApiError("GOOGLE_ADS_DEVELOPER_TOKEN is not set.")
    headers = {"developer-token": token}
    login = login_customer_id or os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login:
        headers["login-customer-id"] = digits(login)
    return headers


def _format_error(error: dict[str, Any]) -> str:
    code = ", ".join(f"{k}={v}" for k, v in error.get("errorCode", {}).items())
    return f"Google Ads API Error: {error.get('message')} ({code})"


def _error(response) -> GoogleAdsApiError:
    request_id = response.headers.get("request-id")
    try:
        payload = response.json()
    except ValueError:
        return GoogleAdsApiError(f"HTTP {response.status_code}: {response.text}")
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    error = payload.get("error", {})
    messages = []
    for detail in error.get("details", []):
        if detail.get("@type", "").endswith(".GoogleAdsFailure"):
            request_id = detail.get("requestId") or request_id
            messages += [_format_error(e) for e in detail.get("errors", [])]
    if not messages:
        messages = [f"HTTP {response.status_code}: {error.get('message', response.text)}"]
    return GoogleAdsApiError(f"Request ID: {request_id}\n" + "\n".join(messages))


def _check(response) -> Any:
    if response.status_code >= 400:
        raise _error(response)
    if not response.content:
        return {}
    return response.json()


def get(path: str, login_customer_id: str | int | None = None) -> Any:
    response = get_session().get(
        f"{BASE}/{path}", headers=_headers(login_customer_id), timeout=TIMEOUT
    )
    return _check(response)


def post(path: str, body: dict[str, Any], login_customer_id: str | int | None = None) -> Any:
    response = get_session().post(
        f"{BASE}/{path}", json=body, headers=_headers(login_customer_id), timeout=TIMEOUT
    )
    return _check(response)


def search_stream(
    customer_id: str, query: str, login_customer_id: str | int | None = None
) -> list[dict[str, Any]]:
    batches = post(
        f"customers/{customer_id}/googleAds:searchStream", {"query": query}, login_customer_id
    )
    return [row for batch in batches for row in batch.get("results", [])]


def search_fields(query: str, login_customer_id: str | int | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    body: dict[str, Any] = {"query": query}
    while True:
        page = post("googleAdsFields:search", body, login_customer_id)
        results += page.get("results", [])
        if not page.get("nextPageToken"):
            return results
        body = {"query": query, "pageToken": page["nextPageToken"]}


def mutate(customer_id: str, operations: list[dict[str, Any]], validate_only: bool) -> list[str]:
    response = post(
        f"customers/{customer_id}/googleAds:mutate",
        {"mutateOperations": operations, "validateOnly": validate_only},
    )
    return [
        next(iter(result.values()))["resourceName"]
        for result in response.get("mutateOperationResponses", [])
    ]
