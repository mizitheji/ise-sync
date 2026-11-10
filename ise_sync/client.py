"""
Thin HTTP client for Cisco ISE's ERS (External RESTful Services) API.

Deliberately NOT touching the `repository` / config-restore CLI path -
everything here is admin-API HTTPS calls (same class of traffic as the
GUI uses), so there is no node reboot involved at any point.

Auth: HTTP Basic against an ERS-enabled admin account. Credentials are
resolved from environment variables per node - never hardcoded.

Performance notes:
  * One pooled keep-alive Session per node (no TCP/TLS handshake per call).
  * Per-object detail GETs are fetched concurrently (bounded by
    `max_workers`) - this is the N+1 hot path when a node has hundreds of
    network devices.
  * Idempotent GETs are retried with backoff on 429/5xx (ISE ERS rate-limits).
    Writes are never auto-retried.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, Union

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.retry import Retry

logger = logging.getLogger("ise_sync.client")

ERS_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}

DEFAULT_WORKERS = 8


class ISEAPIError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class ISEClient:
    def __init__(self, name: str, base_url: str, env_prefix: str,
                 verify_ssl: Union[bool, str] = True, timeout: int = 30,
                 max_workers: int = DEFAULT_WORKERS, retries: int = 3):
        """`verify_ssl` may be True, False, or a path to a CA bundle."""
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = (10, timeout)  # (connect, read)
        self.max_workers = max(1, int(max_workers))

        user = os.environ.get(f"{env_prefix}_USER")
        pw = os.environ.get(f"{env_prefix}_PASS")
        if not user or not pw:
            raise EnvironmentError(
                f"Missing credentials for node '{name}': expected env vars "
                f"{env_prefix}_USER and {env_prefix}_PASS"
            )

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, pw)
        self.session.headers.update(ERS_HEADERS)
        retry = Retry(
            total=retries, backoff_factor=0.5,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),   # never replay writes
            respect_retry_after_header=True, raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=self.max_workers,
                              max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        if verify_ssl is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._cache: dict[str, object] = {}

    # ---------- low level ----------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = self.session.request(method, url, verify=self.verify_ssl,
                                    timeout=self.timeout, **kwargs)
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
            items = data.get("resources", [])
            yield from items
            # stop on last page, or defensively on an empty page (no infinite loop)
            if not items or page * page_size >= data.get("total", 0):
                break
            page += 1

    def ers_get_detail(self, resource: str, obj_id: str) -> dict:
        """Fetch the full object body (not just the summary) by ID."""
        return self._request("GET", f"/ers/config/{resource}/{obj_id}").json()

    def ers_get_all_detail(self, resource: str) -> list[dict]:
        """List + fetch full detail for every object of a resource type.
        Detail GETs run concurrently; result order matches the listing."""
        ids = [s["id"] for s in self.ers_list(resource)]
        if len(ids) <= 1 or self.max_workers == 1:
            return [self.ers_get_detail(resource, i) for i in ids]
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return list(pool.map(lambda i: self.ers_get_detail(resource, i), ids))

    def ers_create(self, resource: str, body: dict) -> requests.Response:
        return self._request("POST", f"/ers/config/{resource}", json=body)

    def ers_update(self, resource: str, obj_id: str, body: dict) -> requests.Response:
        return self._request("PUT", f"/ers/config/{resource}/{obj_id}", json=body)

    def ers_delete(self, resource: str, obj_id: str) -> requests.Response:
        return self._request("DELETE", f"/ers/config/{resource}/{obj_id}")

    # ---------- cached lookups ----------

    def _identity_groups(self) -> list[dict]:
        """One listing serves both id->name and name->id (was two separate calls)."""
        if "ig" not in self._cache:
            self._cache["ig"] = [i for i in self.ers_list("identitygroup")
                                 if "id" in i and "name" in i]
        return self._cache["ig"]  # type: ignore[return-value]

    def invalidate_identity_groups(self) -> None:
        """Call after creating identity groups so later lookups see them."""
        self._cache.pop("ig", None)

    def get_identity_group_id_to_name(self) -> dict[str, str]:
        return {i["id"]: i["name"] for i in self._identity_groups()}

    def get_identity_group_name_to_id(self) -> dict[str, str]:
        return {i["name"]: i["id"] for i in self._identity_groups()}

    def get_admin_user_names(self) -> set[str]:
        """Return a cached set of admin user names on this node."""
        if "admins" not in self._cache:
            try:
                self._cache["admins"] = {i["name"] for i in self.ers_list("adminuser")
                                         if "name" in i}
            except Exception as e:
                logger.warning("[%s] Failed to fetch adminuser list: %s", self.name, e)
                self._cache["admins"] = set()
        return self._cache["admins"]  # type: ignore[return-value]

    # ---------- Open API (e.g. policy sets - ERS doesn't cover these) ----------

    def openapi_list(self, path: str) -> list[dict]:
        """List endpoints return full objects inline under a 'response' key -
        unlike ERS there's no separate summary+detail step."""
        data = self._request("GET", f"/api/v1/{path}").json()
        if isinstance(data, dict):
            return data.get("response", [])
        return data if isinstance(data, list) else []

    def openapi_create(self, path: str, body: dict) -> requests.Response:
        return self._request("POST", f"/api/v1/{path}", json=body)

    def openapi_update(self, path: str, obj_id: str, body: dict) -> requests.Response:
        return self._request("PUT", f"/api/v1/{path}/{obj_id}", json=body)

    def openapi_delete(self, path: str, obj_id: str) -> requests.Response:
        return self._request("DELETE", f"/api/v1/{path}/{obj_id}")
