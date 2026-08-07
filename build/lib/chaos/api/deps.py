"""Shared FastAPI dependencies: sessions, bus access and role-based identity."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from chaos.config import Settings, get_settings
from chaos.db import get_session_factory
from chaos.mqtt import MessageBus

#: SDD section 15.2 role model, least privilege first.
ROLES = ("viewer", "operator", "maintainer", "administrator")
_ROLE_RANK = {role: index for index, role in enumerate(ROLES)}


@dataclass(frozen=True)
class Principal:
    """The authenticated actor behind a request."""

    name: str
    role: str = "viewer"
    kind: str = "human"

    def has_role(self, minimum: str) -> bool:
        return _ROLE_RANK.get(self.role, -1) >= _ROLE_RANK[minimum]


def get_db(request: Request) -> Iterator[Session]:
    factory = getattr(request.app.state, "session_factory", None) or get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_bus(request: Request) -> MessageBus:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:  # pragma: no cover - defensive
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Message bus unavailable")
    return bus


def get_app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def get_principal(
    x_operator: Annotated[str | None, Header(alias="X-Operator")] = None,
    x_operator_role: Annotated[str | None, Header(alias="X-Operator-Role")] = None,
) -> Principal:
    """Resolve the caller.

    Local-first deployments terminate authentication at the reverse proxy / VPN
    (SDD section 15.3). The platform still records a named actor on every
    audited action, so an unnamed caller is a viewer and can never write.
    """
    if not x_operator:
        return Principal(name="anonymous", role="viewer", kind="human")
    role = (x_operator_role or "viewer").lower()
    if role not in _ROLE_RANK:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown role: {role}")
    return Principal(name=x_operator, role=role)


def require_role(minimum: str):
    """Dependency factory enforcing a minimum role."""
    if minimum not in _ROLE_RANK:  # pragma: no cover - programmer error
        raise ValueError(f"Unknown role: {minimum}")

    def _dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if not principal.has_role(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Role '{minimum}' or higher required (caller has '{principal.role}')",
            )
        return principal

    return _dependency


DbSession = Annotated[Session, Depends(get_db)]
Bus = Annotated[MessageBus, Depends(get_bus)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
OperatorPrincipal = Annotated[Principal, Depends(require_role("operator"))]
MaintainerPrincipal = Annotated[Principal, Depends(require_role("maintainer"))]
AdminPrincipal = Annotated[Principal, Depends(require_role("administrator"))]
