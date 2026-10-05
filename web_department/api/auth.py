"""Bearer-token auth with three roles (SRS 2.3): requester, approver, operator.

Tokens come from environment variables for now (Settings.*_token); a real identity provider is
future work. ``UserRole`` is distinct from ``config.permissions.Role``, which governs what worker
roles (Frontend, Backend, QA, ...) may do inside a workstation, not who may call this API.
"""

import secrets
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from web_department.config.settings import Settings, get_settings

_security = HTTPBearer(auto_error=False)


class UserRole(StrEnum):
    """A human or system user class interacting with the API (SRS 2.3)."""

    REQUESTER = "requester"
    APPROVER = "approver"
    OPERATOR = "operator"


def _configured_tokens(settings: Settings) -> dict[UserRole, str]:
    pairs = (
        (UserRole.REQUESTER, settings.requester_token),
        (UserRole.APPROVER, settings.approver_token),
        (UserRole.OPERATOR, settings.operator_token),
    )
    return {role: token.get_secret_value() for role, token in pairs if token is not None}


async def get_current_role(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_security)],
) -> UserRole:
    """Resolve the caller's bearer token to a role, or raise 401."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    for role, token in _configured_tokens(get_settings()).items():
        if secrets.compare_digest(token, credentials.credentials):
            return role
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")


def require_role(*allowed: UserRole) -> Callable[[UserRole], Awaitable[UserRole]]:
    """Build a dependency that requires the caller's role to be one of ``allowed``."""

    async def dependency(role: Annotated[UserRole, Depends(get_current_role)]) -> UserRole:
        if role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return role

    return dependency
