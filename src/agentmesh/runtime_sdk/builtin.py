"""Deterministic descriptors shared by bootstrap and outward adapters."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

LANGGRAPH_DESCRIPTOR: dict[str, Any] = {
    "schema_name": "agentmesh.runtime-descriptor",
    "schema_version": 1,
    "runtime_key": "agentmesh.langgraph",
    "display_name": "LangGraph Runtime",
    "adapter_kind": "python-in-process",
    "capabilities": {
        "execution_mode": ["inline", "managed_async"],
        "reattach": True,
        "cancel": "cooperative",
        "pause_resume": True,
        "checkpoint": True,
        "fork": True,
        "event_stream": True,
        "tool_bridge": ["governed_action_v1"],
        "artifact_io": ["reference"],
        "isolation_profiles": ["trusted-in-process"],
        "modalities": ["text", "structured"],
    },
    "limits": {
        "max_assignment_bytes": 262144,
        "max_event_bytes": 65536,
        "max_result_bytes": 262144,
        "max_artifact_refs": 128,
    },
}


def langgraph_descriptor() -> dict[str, Any]:
    """Return the immutable A1 v1 descriptor for compatibility/audit."""
    return deepcopy(LANGGRAPH_DESCRIPTOR)


LANGGRAPH_V2_DESCRIPTOR: dict[str, Any] = {
    "schema_name": "agentmesh.runtime-descriptor",
    "schema_version": 1,
    "runtime_key": "agentmesh.langgraph",
    "display_name": "LangGraph Runtime (deterministic inline)",
    "adapter_kind": "python-in-process",
    "capabilities": {
        "execution_mode": ["inline"],
        "reattach": False,
        "cancel": "none",
        "pause_resume": False,
        "checkpoint": False,
        "fork": False,
        "event_stream": False,
        "tool_bridge": ["governed_action_v1"],
        "artifact_io": ["reference"],
        "isolation_profiles": ["trusted-in-process"],
        "modalities": ["text", "structured"],
    },
    "limits": dict(LANGGRAPH_DESCRIPTOR["limits"]),
}


def langgraph_v2_descriptor() -> dict[str, Any]:
    """Return the honest A2 deterministic-inline descriptor."""
    return deepcopy(LANGGRAPH_V2_DESCRIPTOR)
