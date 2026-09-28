from __future__ import annotations

from ipaddress import ip_address
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from agentmesh.api.security import PrincipalDependency
from agentmesh.application.model_connection_services import ModelConnectionService
from agentmesh.domain.identity import Permission

router = APIRouter(prefix="/api/v1/model-connections", tags=["model-connections"])


class CredentialInput(BaseModel):
    type: str = Field(pattern="^(api_key|environment)$")
    value: str | None = Field(default=None, max_length=4096)
    name: str | None = Field(default=None, max_length=128)


class ConnectionInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    provider: str | None = Field(default=None, pattern="^(openai|deepseek)$")
    model: str | None = Field(default=None, min_length=1, max_length=128)
    endpoint: str | None = Field(default=None, max_length=512)
    credential: CredentialInput | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_update_values(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("Explicit null fields are not supported")
        return value

    def payload(self) -> dict[str, Any]:
        value = self.model_dump(exclude_unset=True)
        if "credential" in value and value["credential"] is not None:
            value["credential"] = {k: v for k, v in value["credential"].items() if v is not None}
        return value


def _service(request: Request) -> ModelConnectionService:
    return request.app.state.container.model_connection_service


def _authorize(request: Request, principal: Any, *, write: bool) -> None:
    identity = request.app.state.container.identity_service
    if not identity.enabled or not principal.authenticated:
        raise HTTPException(status_code=403, detail="Authenticated Identity RBAC is required")
    try:
        identity.authorize(
            principal, Permission.CREDENTIAL_MANAGE if write else Permission.CREDENTIAL_READ
        )
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Model connection access denied") from exc
    if write:
        peer = request.client.host if request.client else ""
        try:
            loopback = ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if request.url.scheme != "https" and not loopback:
            raise HTTPException(
                status_code=400, detail="Secret operations require HTTPS or loopback"
            )


@router.get("")
def list_model_connections(request: Request, principal: PrincipalDependency) -> dict[str, Any]:
    service = _service(request)
    if not request.app.state.container.identity_service.enabled:
        return {
            "connections": [],
            "readiness": {"ready": False, "reason": "identity_rbac must be enabled"},
        }
    _authorize(request, principal, write=False)
    connections = service.list()
    ready = any(
        item["enabled"] and (item["credential_source"] == "environment" or service.ready)
        for item in connections
    )
    reason = (
        None
        if ready
        else "model connection encryption key is not configured"
        if any(item["enabled"] and item["credential_source"] == "api_key" for item in connections)
        and not service.ready
        else "no enabled model connections are configured"
    )
    return {
        "connections": connections,
        "readiness": {
            "ready": ready,
            "reason": reason,
        },
    }


@router.post("")
def create_model_connection(
    body: ConnectionInput, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    _authorize(request, principal, write=True)
    try:
        payload = body.payload()
        if not all(payload.get(key) for key in ("name", "provider", "credential")):
            raise HTTPException(
                status_code=422, detail="Name, provider, and credential are required"
            )
        return _service(request).create(payload)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{connection_id}")
def update_model_connection(
    connection_id: UUID, body: ConnectionInput, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    _authorize(request, principal, write=True)
    try:
        return _service(request).update(connection_id, body.payload())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Model connection not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{connection_id}/disable")
def disable_model_connection(
    connection_id: UUID, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    _authorize(request, principal, write=True)
    try:
        return _service(request).disable(connection_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Model connection not found") from exc


@router.post("/{connection_id}/test")
def test_model_connection(
    connection_id: UUID, request: Request, principal: PrincipalDependency
) -> dict[str, Any]:
    _authorize(request, principal, write=True)
    try:
        return _service(request).test(connection_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Model connection not found") from exc
