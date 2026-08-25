from datetime import timedelta

from autonomy.models import utc_now
from side_effects.activation import (
    GitHubReadinessResult,
    READINESS_BLOCKED,
    READINESS_PARTIAL,
    READINESS_READY,
    READINESS_UNKNOWN,
    RepositoryReadiness,
    WRITE_PERMISSION_DENIED,
    WRITE_PERMISSION_UNCONFIRMED,
    WRITE_PERMISSION_UNKNOWN,
)
from side_effects.github.errors import GitHubAdapterError


class GitHubReadinessProbe:
    """Read-only repository metadata probe. Never mutates. Never claims write confirmed."""

    def __init__(self, transport, *, timeout_seconds: float = 15.0):
        self._transport = transport
        self._timeout = float(timeout_seconds)

    async def probe(
        self, repositories: tuple[str, ...], *, ttl_seconds: float | None, now=None
    ) -> GitHubReadinessResult:
        stamp = now or utc_now()
        expires = None
        if ttl_seconds is not None:
            expires = stamp + timedelta(seconds=float(ttl_seconds))
        rows = []
        authenticated = True
        write_status = WRITE_PERMISSION_UNCONFIRMED
        reason = "probe_ok"
        for repo_ref in repositories:
            owner, _, repo = str(repo_ref).partition("/")
            row, auth_ok, row_write, row_reason = await self._probe_one(owner, repo)
            rows.append(row)
            authenticated = authenticated and auth_ok
            if row_write == WRITE_PERMISSION_DENIED:
                write_status = WRITE_PERMISSION_DENIED
            if row_reason in {
                "github_probe_rate_limited",
                "github_authentication_failed",
                "github_probe_timeout",
            }:
                reason = row_reason
        accessible_count = sum(1 for row in rows if row.accessible)
        if not rows:
            status = READINESS_UNKNOWN
            reason = "github_allowlist_empty"
            write_status = WRITE_PERMISSION_UNKNOWN
            authenticated = False
        elif accessible_count == len(rows):
            status = READINESS_READY
        elif accessible_count == 0:
            status = READINESS_BLOCKED
            if reason == "probe_ok":
                reason = "github_repository_inaccessible"
        else:
            status = READINESS_PARTIAL
            reason = "github_readiness_partial"
        if write_status == WRITE_PERMISSION_DENIED:
            status = READINESS_BLOCKED
        if reason == "github_probe_rate_limited":
            status = READINESS_BLOCKED
            write_status = WRITE_PERMISSION_UNKNOWN
        if reason == "github_probe_timeout":
            status = READINESS_UNKNOWN
            write_status = WRITE_PERMISSION_UNKNOWN
        if reason == "github_authentication_failed":
            status = READINESS_BLOCKED
            authenticated = False
            write_status = WRITE_PERMISSION_UNKNOWN
        return GitHubReadinessResult(
            status=status,
            authenticated=authenticated and status != READINESS_UNKNOWN,
            repository_accessible=accessible_count == len(rows) and bool(rows),
            write_permission_status=write_status,
            checked_at=stamp,
            expires_at=expires,
            repository_results=tuple(rows),
            reason_code=reason,
            metadata={"repository_count": len(rows), "accessible_count": accessible_count},
        )

    async def _probe_one(self, owner: str, repo: str):
        ref = f"{owner}/{repo}".lower()
        try:
            await self._transport.get_repository(owner, repo)
        except GitHubAdapterError as exc:
            code = exc.error_code
            if code == "github_rate_limited":
                code = "github_probe_rate_limited"
            elif code == "github_timeout_uncertain":
                code = "github_probe_timeout"
            auth_ok = code != "github_authentication_failed"
            write = WRITE_PERMISSION_UNKNOWN
            if code == "github_permission_denied":
                write = WRITE_PERMISSION_DENIED
            accessible = False
            authenticated = auth_ok
            return (
                RepositoryReadiness(
                    repository_ref=ref,
                    accessible=accessible,
                    authenticated=authenticated,
                    reason_code=code,
                    write_permission_status=write,
                ),
                authenticated,
                write,
                code,
            )
        return (
            RepositoryReadiness(
                repository_ref=ref,
                accessible=True,
                authenticated=True,
                reason_code="repository_accessible",
                write_permission_status=WRITE_PERMISSION_UNCONFIRMED,
            ),
            True,
            WRITE_PERMISSION_UNCONFIRMED,
            "probe_ok",
        )
