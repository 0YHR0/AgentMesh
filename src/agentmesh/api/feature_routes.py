from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from agentmesh.features import Feature, FeatureGateSet, FeatureProfile

router = APIRouter(prefix="/api/v1", tags=["system"])


class FeatureStateResponse(BaseModel):
    name: Feature
    enabled: bool
    description: str
    dependencies: list[Feature]


class FeatureGateResponse(BaseModel):
    profile: FeatureProfile
    restart_required: bool
    features: list[FeatureStateResponse]


def get_feature_gates(request: Request) -> FeatureGateSet:
    return request.app.state.container.feature_gates


FeatureGatesDependency = Annotated[FeatureGateSet, Depends(get_feature_gates)]


def require_feature(feature: Feature):
    def dependency(feature_gates: FeatureGatesDependency) -> None:
        feature_gates.require(feature)

    return dependency


@router.get("/features", response_model=FeatureGateResponse)
def list_features(feature_gates: FeatureGatesDependency) -> FeatureGateResponse:
    return FeatureGateResponse(
        profile=feature_gates.profile,
        restart_required=True,
        features=[
            FeatureStateResponse(
                name=state.feature,
                enabled=state.enabled,
                description=state.description,
                dependencies=list(state.dependencies),
            )
            for state in feature_gates.states()
        ],
    )
