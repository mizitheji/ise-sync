"""
Thin HTTP client for Cisco ISE's ERS (External RESTful Services) API.

Deliberately NOT touching the `repository` / config-restore CLI path -
everything here is admin-API HTTPS calls (same class of traffic as the
GUI uses), so there is no node reboot involved at any point.

Auth: HTTP Basic against an ERS-enabled admin account. Credentials are
resolved from environment variables per node - never hardcoded, same
convention as netconf-backup.
"""
from __future__ import annotations

import os
import logging
from typing import Any, Iterator

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger("ise_sync.client")

ERS_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class ISEAPIError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class ISEClient:
    def __init__(self, name: str, base_url: str, env_prefix: str, verify_ssl: bool = False,
                 timeout: int = 30):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        user = os.environ.get(f"{env_prefix}_USER")
        pw = os.environ.get(f"{env_prefix}_PASS")
        if not user or not pw:
            raise EnvironmentError(
                f"Missing credentials for node '{name}': expected env vars "
                f"{env_prefix}_USER and {env_prefix}_PASS"
            )
        self.auth = HTTPBasicAuth(user, pw)
        self.session = requests.Session()

        if not verify_ssl:
            requests.packages.urllib3.disable_warnings()  # noqa

    # ---------- low level ----------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = self.session.request(
            method, url, auth=self.auth, headers=ERS_HEADERS,
            verify=self.verify_ssl, timeout=self.timeout, **kwargs,
        )
        if resp.status_code >= 400:
            raise ISEAPIError(method, url, resp.status_code, resp.text)
        return resp

    # ---------- ERS (config objects: NDG, identity groups, authz profiles, etc.) ----------

    def ers_list(self, resource: str, page_size: int = 100) -> Iterator[dict]:
        """Yield summary objects ({id, name, description, link}) for an ERS resource,
        paginating through all pages."""
        page = 1
        while True:
            resp = self._request(
                "GET", f"/ers/config/{resource}",
                params={"size": page_size, "page": page},
            )
            data = resp.json().get("SearchResult", {})
            for item in data.get("resources", []):
                yield item
            total = data.get("total", 0)
            if page * page_size >= total:
                break
            page += 1

    def ers_get_detail(self, resource: str, obj_id: str) -> dict:
        """Fetch the full object body (not just the summary) by ID."""
        resp = self._request("GET", f"/ers/config/{resource}/{obj_id}")
        return resp.json()

    def ers_get_all_detail(self, resource: str) -> list[dict]:
        """List + fetch full detail for every object of a resource type."""
        out = []
        for summary in self.ers_list(resource):
            out.append(self.ers_get_detail(resource, summary["id"]))
        return out

    def ers_create(self, resource: str, body: dict) -> requests.Response:
        return self._request("POST", f"/ers/config/{resource}", json=body)

    def ers_update(self, resource: str, obj_id: str, body: dict) -> requests.Response:
        return self._request("PUT", f"/ers/config/{resource}/{obj_id}", json=body)

    def ers_delete(self, resource: str, obj_id: str) -> requests.Response:
        return self._request("DELETE", f"/ers/config/{resource}/{obj_id}")

    # ---------- Open API (e.g. policy sets - confirmed working, ERS doesn't cover these) ----------

    def openapi_list(self, path: str) -> list[dict]:
        """List endpoints return full objects inline under a 'response' key -
        unlike ERS there's no separate summary+detail step."""
        resp = self._request("GET", f"/api/v1/{path}")
        data = resp.json()
        if isinstance(data, dict):
            return data.get("response", [])
        return data if isinstance(data, list) else []

    def openapi_create(self, path: str, body: dict) -> requests.Response:
        return self._request("POST", f"/api/v1/{path}", json=body)

    def openapi_update(self, path: str, obj_id: str, body: dict) -> requests.Response:
        return self._request("PUT", f"/api/v1/{path}/{obj_id}", json=body)
