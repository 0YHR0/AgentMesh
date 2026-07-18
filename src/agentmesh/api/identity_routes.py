from fastapi import APIRouter, Depends
from pydantic import BaseModel

from agentmesh.api.feature_routes import require_feature
from agentmesh.api.security import PrincipalDependency, require_permission
from agentmesh.domain.identity import Permission, PrincipalType, Role
from agentmesh.features import Feature

router = APIRouter(
    prefix="/api/v1",
    tags=["identity"],
    dependencies=[
        Depends(require_permission(Permission.SYSTEM_INSPECT)),
        Depends(require_feature(Feature.IDENTITY_RBAC)),
    ],
)


class PrincipalContextResponse(BaseModel):
    principal_id: str
    tenant_id: str
    principal_type: PrincipalType
    roles: list[Role]
    authentication_method: str


@router.get("/identity/me", response_model=PrincipalContextResponse)
def get_current_principal(principal: PrincipalDependency) -> PrincipalContextResponse:
    return PrincipalContextResponse(
        principal_id=principal.principal_id,
        tenant_id=principal.tenant_id,
        principal_type=principal.principal_type,
        roles=sorted(principal.roles, key=lambda value: value.value),
        authentication_method=principal.authentication_method,
    )
