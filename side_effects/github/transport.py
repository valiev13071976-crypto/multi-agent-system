import asyncio
from typing import Protocol
from urllib.parse import quote

import httpx

from side_effects.github.errors import GitHubAdapterError
from side_effects.github.models import GITHUB_API_BASE


class GitHubTransport(Protocol):
    async def get_issue_labels(
        self, owner: str, repo: str, issue_number: int
    ) -> tuple[str, ...]: ...

    async def add_label(
        self, owner: str, repo: str, issue_number: int, label: str
    ) -> None: ...

    async def remove_label(
        self, owner: str, repo: str, issue_number: int, label: str
    ) -> None: ...

    async def get_repository(self, owner: str, repo: str) -> dict: ...


def map_github_status(status: int, *, remaining: str | None = None) -> str:
    if status in {401}:
        return "github_authentication_failed"
    if status == 429:
        return "github_rate_limited"
    if status == 403:
        if remaining == "0":
            return "github_rate_limited"
        return "github_permission_denied"
    if status == 404:
        return "github_resource_not_found_or_inaccessible"
    if status in {409}:
        return "github_request_conflict"
    if status == 422:
        return "github_validation_error"
    if status >= 500:
        return "github_temporary_error"
    return "github_http_error"


class FakeGitHubTransport:
    """In-memory GitHub Issues labels. No network."""

    def __init__(self):
        self._labels: dict[tuple[str, str, int], list[str]] = {}
        self.get_calls = 0
        self.add_calls = 0
        self.remove_calls = 0
        self.hang_get = False
        self.hang_mutate = False
        self.hang_verify = False
        self.get_status = 200
        self.mutate_status = 200
        self.contradict_after = False
        self.verify_gets = 0
        self.remove_not_found = False
        self.get_repository_calls = 0
        self.hang_probe = False
        self._repo_status: dict[tuple[str, str], int] = {}

    def seed(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> None:
        self._labels[(owner.lower(), repo.lower(), int(issue_number))] = list(labels)

    def current(self, owner: str, repo: str, issue_number: int) -> tuple[str, ...]:
        return tuple(self._labels.get((owner.lower(), repo.lower(), int(issue_number)), []))

    async def get_issue_labels(self, owner, repo, issue_number) -> tuple[str, ...]:
        self.get_calls += 1
        if self.hang_get:
            await asyncio.sleep(3600)
        if self.hang_verify and self.get_calls > 1:
            self.verify_gets += 1
            await asyncio.sleep(3600)
        if self.get_status != 200:
            raise GitHubAdapterError(map_github_status(self.get_status))
        return tuple(self.current(owner, repo, issue_number))

    async def add_label(self, owner, repo, issue_number, label) -> None:
        if self.hang_mutate:
            await asyncio.sleep(3600)
        if self.mutate_status != 200:
            raise GitHubAdapterError(map_github_status(self.mutate_status))
        self.add_calls += 1
        key = (owner.lower(), repo.lower(), int(issue_number))
        current = self._labels.setdefault(key, [])
        if not any(item.casefold() == label.casefold() for item in current):
            if self.contradict_after:
                return
            current.append(label)

    async def remove_label(self, owner, repo, issue_number, label) -> None:
        if self.hang_mutate:
            await asyncio.sleep(3600)
        if self.mutate_status != 200:
            raise GitHubAdapterError(map_github_status(self.mutate_status))
        self.remove_calls += 1
        if self.remove_not_found:
            raise GitHubAdapterError("github_resource_not_found_or_inaccessible")
        key = (owner.lower(), repo.lower(), int(issue_number))
        current = self._labels.get(key, [])
        if self.contradict_after:
            return
        self._labels[key] = [
            item for item in current if item.casefold() != label.casefold()
        ]

    def seed_repository(self, owner: str, repo: str, status: int = 200) -> None:
        self._repo_status[(owner.lower(), repo.lower())] = int(status)

    async def get_repository(self, owner, repo) -> dict:
        self.get_repository_calls += 1
        if self.hang_probe:
            await asyncio.sleep(3600)
        status = self._repo_status.get((owner.lower(), repo.lower()), 200)
        if status != 200:
            raise GitHubAdapterError(map_github_status(status))
        return {"full_name": f"{owner}/{repo}"}


class GitHubHttpTransport:
    """HTTPS transport bound to api.github.com. Token stays inside this object."""

    def __init__(self, token: str, *, timeout_seconds: float = 15.0, client=None):
        if not token or not str(token).strip():
            raise GitHubAdapterError("github_authentication_failed")
        self._token = str(token)
        self._timeout = float(timeout_seconds)
        self._client = client
        self._base = GITHUB_API_BASE

    def __repr__(self) -> str:
        return "GitHubHttpTransport(token=[REDACTED])"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "panda-side-effect-github-labels",
        }

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            raise GitHubAdapterError("github_invalid_path")
        return f"{self._base}{path}"

    async def _request(self, method: str, path: str, json_body=None):
        headers = self._headers()
        url = self._url(path)
        client = self._client
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=self._timeout) as owned:
                    response = await owned.request(
                        method, url, headers=headers, json=json_body
                    )
            else:
                response = await client.request(
                    method, url, headers=headers, json=json_body, timeout=self._timeout
                )
        except httpx.TimeoutException:
            raise GitHubAdapterError("github_timeout_uncertain") from None
        except httpx.RequestError:
            raise GitHubAdapterError("github_network_error") from None
        if response.status_code >= 400:
            remaining = None
            headers_obj = getattr(response, "headers", {}) or {}
            remaining = headers_obj.get("x-ratelimit-remaining") or headers_obj.get(
                "X-RateLimit-Remaining"
            )
            raise GitHubAdapterError(
                map_github_status(int(response.status_code), remaining=remaining)
            )
        return response

    async def get_issue_labels(self, owner, repo, issue_number) -> tuple[str, ...]:
        path = f"/repos/{owner}/{repo}/issues/{int(issue_number)}/labels"
        response = await self._request("GET", path)
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubAdapterError("github_validation_error")
        names = []
        for row in payload:
            if isinstance(row, dict) and row.get("name"):
                names.append(str(row["name"]))
            elif isinstance(row, str):
                names.append(row)
        return tuple(names)

    async def add_label(self, owner, repo, issue_number, label) -> None:
        path = f"/repos/{owner}/{repo}/issues/{int(issue_number)}/labels"
        await self._request("POST", path, json_body={"labels": [label]})

    async def remove_label(self, owner, repo, issue_number, label) -> None:
        encoded = quote(str(label), safe="")
        path = f"/repos/{owner}/{repo}/issues/{int(issue_number)}/labels/{encoded}"
        try:
            await self._request("DELETE", path)
        except GitHubAdapterError as exc:
            if exc.error_code != "github_resource_not_found_or_inaccessible":
                raise
        # Follow-up GET is owned by the adapter, not this method.

    async def get_repository(self, owner, repo) -> dict:
        path = f"/repos/{owner}/{repo}"
        response = await self._request("GET", path)
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAdapterError("github_validation_error")
        name = payload.get("full_name") or f"{owner}/{repo}"
        return {"full_name": str(name)}
