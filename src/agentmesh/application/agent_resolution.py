"""Small application-level Agent lookup helpers shared by progression components."""

from __future__ import annotations

from typing import Any

from agentmesh.domain.errors import AgentUnavailable
from agentmesh.domain.registry import (
    AgentDefinitionLifecycle,
    AgentVersion,
    AgentVersionStatus,
    normalize_agent_name,
)


def version_satisfies(version: AgentVersion | None, required: set[str]) -> bool:
    """Return whether a published asynchronous Agent satisfies capabilities."""
    return bool(
        version is not None
        and version.status == AgentVersionStatus.PUBLISHED
        and version.content_digest
        and "async" in version.execution_modes
        and required.issubset(version.verified_capabilities)
    )


def resolve_default_agent(
    uow: Any,
    tenant_id: str,
    configured_name: str,
) -> tuple[str, AgentVersion]:
    """Resolve the legacy/default Agent contract.

    This intentionally only requires a published, digest-bearing default
    version.  The ordinary TaskApplicationService contract predates runtime
    capability admission and must retain that compatibility.
    """
    name = normalize_agent_name(configured_name)
    definition = uow.agent_definitions.get_by_name(tenant_id, name, for_update=True)
    if definition is None or definition.default_version_id is None:
        raise AgentUnavailable(f"Agent {name} has no published default version")
    version = uow.agent_versions.get(definition.default_version_id, for_update=True)
    if (
        version is None
        or version.status != AgentVersionStatus.PUBLISHED
        or not version.content_digest
    ):
        raise AgentUnavailable(f"Agent {name} default version is unavailable")
    return definition.name, version


def resolve_capable_agent(
    uow: Any,
    tenant_id: str,
    configured_name: str,
    required: set[str],
) -> tuple[str, AgentVersion]:
    """Resolve one active Agent's published default version under repository locks."""
    name = normalize_agent_name(configured_name)
    definition = uow.agent_definitions.get_by_name(tenant_id, name, for_update=True)
    if (
        definition is None
        or definition.lifecycle != AgentDefinitionLifecycle.ACTIVE
        or definition.default_version_id is None
    ):
        raise AgentUnavailable(f"Agent {name} has no active published default version")
    version = uow.agent_versions.get(definition.default_version_id, for_update=True)
    if not version_satisfies(version, required):
        capabilities = ", ".join(sorted(required))
        raise AgentUnavailable(f"Agent {name} does not satisfy capabilities: {capabilities}")
    return definition.name, version


def resolve_named_agent(
    uow: Any,
    tenant_id: str,
    configured_name: str,
    required: set[str],
) -> tuple[str, AgentVersion]:
    """Backward-compatible alias for the capability-admission contract."""
    return resolve_capable_agent(uow, tenant_id, configured_name, required)


__all__ = [
    "resolve_capable_agent",
    "resolve_default_agent",
    "resolve_named_agent",
    "version_satisfies",
]
