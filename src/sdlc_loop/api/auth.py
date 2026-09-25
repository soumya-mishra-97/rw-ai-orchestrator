"""API-key based role check.

Roles: ``operator`` starts runs, ``reviewer`` resumes escalated runs (the
human-in-the-loop decision), ``auditor`` reads runs and audit trails. Keys come
from ``SDLC_API_KEYS="key:role,key:role"``. With no keys configured the API runs
in open local-dev mode and says so in every response header — production would
swap this dependency for OIDC/JWT validation against the corporate IdP.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request, status

Role = Literal["operator", "reviewer", "auditor"]

# Which roles may perform which capability (reviewers can also read, etc.).
_GRANTS: dict[Role, set[Role]] = {
    "operator": {"operator", "auditor"},
    "reviewer": {"reviewer", "auditor"},
    "auditor": {"auditor"},
}


@dataclass(frozen=True, slots=True)
class Principal:
    name: str


def parse_keys(raw: str) -> dict[str, Role]:
    keys: dict[str, Role] = {}
    for item in filter(None, (p.strip() for p in raw.split(","))):
        key, _, role = item.partition(":")
        if role not in _GRANTS:
            raise ValueError(f"unknown role {role!r} in SDLC_API_KEYS")
        keys[key] = role
    return keys


def require(role: Role) -> Callable[..., Principal]:
    def dependency(
        request: Request, x_api_key: Annotated[str | None, Header()] = None
    ) -> Principal:
        keys: dict[str, Role] = request.app.state.api_keys
        if not keys:
            return Principal("local-dev")
        for key, key_role in keys.items():
            if x_api_key and hmac.compare_digest(key, x_api_key):
                if role in _GRANTS[key_role]:
                    return Principal(f"{key_role}:{key[:4]}…")
                raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")

    return dependency


RequireReviewer = Annotated[Principal, Depends(require("reviewer"))]
