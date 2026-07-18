from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agentmesh.application.identity_services import IdentityService
from agentmesh.domain.identity import Permission, PrincipalContext

bearer_scheme = HTTPBearer(auto_error=False)
BearerCredential = Annotated[
    HTTPAuthorizationCredentials | None,
    Security(bearer_scheme),
]


def get_identity_service(request: Request) -> IdentityService:
    return request.app.state.container.identity_service


IdentityServiceDependency = Annotated[IdentityService, Depends(get_identity_service)]


def get_principal_context(
    identity: IdentityServiceDependency,
    credential: BearerCredential = None,
) -> PrincipalContext:
    authorization = (
        f"{credential.scheme} {credential.credentials}" if credential is not None else None
    )
    return identity.authenticate(authorization)


PrincipalDependency = Annotated[PrincipalContext, Depends(get_principal_context)]


def require_permission(permission: Permission):
    def dependency(
        principal: PrincipalDependency,
        identity: IdentityServiceDependency,
    ) -> PrincipalContext:
        identity.authorize(principal, permission)
        return principal

    return dependency


def require_read_or_write_permission(
    read_permission: Permission,
    write_permission: Permission,
):
    def dependency(
        request: Request,
        principal: PrincipalDependency,
        identity: IdentityServiceDependency,
    ) -> PrincipalContext:
        permission = (
            read_permission if request.method in {"GET", "HEAD", "OPTIONS"} else write_permission
        )
        identity.authorize(principal, permission)
        return principal

    return dependency
