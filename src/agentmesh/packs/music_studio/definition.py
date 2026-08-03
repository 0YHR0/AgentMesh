from __future__ import annotations

from copy import deepcopy
from typing import Any

from agentmesh.domain.company_packs import CompanyPack
from agentmesh.domain.errors import InvalidCompanyPack
from agentmesh.packs.sdk import CompanyTemplateDefinition

TEMPLATE_SLUG = "music-studio"
PACK_KEY = "agentmesh.music-studio"
PACK_VERSION = "0.3.0"
PACK_NAME = "AgentMesh Music Studio"
DEFAULT_MISSION = "Turn creative intent into original, reviewed, traceable music."
USE_PLANS = ("internal-demo", "personal", "commercial-review")


def _unit(key: str, name: str, purpose: str) -> dict[str, Any]:
    return {
        "kind": "organization_unit",
        "key": key,
        "name": name,
        "purpose": purpose,
        "memory_namespace": f"company/music/{key}",
    }


def _position(
    key: str,
    unit_key: str,
    title: str,
    outcome: str,
    capabilities: list[str],
    *,
    reports_to: str | None = None,
    tools: list[str] | None = None,
    approval_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "position",
        "key": key,
        "unit_key": unit_key,
        "title": title,
        "responsibility_contract": {
            "outcome": outcome,
            "evidence_required": True,
            "may_self_approve": False,
        },
        "required_capabilities": capabilities,
        "allowed_tool_capabilities": tools or [],
        "approval_scope": approval_scope or {},
        "budget_scope": {},
    }
    if reports_to:
        value["reports_to_key"] = reports_to
    return value


def _object_type(
    key: str,
    name: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    review_position: str,
    extra_actions: dict[str, Any] | None = None,
    schema_version: int = 1,
) -> dict[str, Any]:
    actions = {
        "submit": {
            "from": ["DRAFT"],
            "to": "IN_REVIEW",
            "allowed_update_fields": [],
            "required_evidence": True,
        },
        "approve": {
            "from": ["IN_REVIEW"],
            "to": "APPROVED",
            "allowed_update_fields": [],
            "required_evidence": True,
            "required_position_keys": [review_position],
        },
        "retire": {
            "from": ["APPROVED"],
            "to": "RETIRED",
            "allowed_update_fields": [],
            "required_position_keys": [review_position],
        },
    }
    actions.update(extra_actions or {})
    return {
        "kind": "business_object_type",
        "key": key,
        "name": name,
        "schema_version": schema_version,
        "json_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "lifecycle_definition": {
            "states": ["DRAFT", "IN_REVIEW", "APPROVED", "RETIRED"],
            "initial_state": "DRAFT",
            "actions": actions,
        },
        "ownership_rules": {"review_position_key": review_position},
        "retention_policy": {"minimum_days": 365},
    }


def manifest() -> dict[str, Any]:
    resources: list[dict[str, Any]] = [
        _unit("creative-direction", "Creative Direction", "Own the brief and final quality."),
        _unit("trend-lab", "A&R and Trend Lab", "Translate authorized evidence into attributes."),
        _unit("songwriting", "Songwriting", "Create and revise original lyrics."),
        _unit("production", "Production", "Design and execute generation specifications."),
        _unit("listening", "Listening Room", "Evaluate actual audio and propose changes."),
        _position(
            "owner",
            "creative-direction",
            "Owner",
            "The final work reflects the declared intent and receives explicit approval.",
            ["music.release.approve", "company.governance"],
            approval_scope={"final_release": True},
        ),
        _position(
            "creative-director",
            "creative-direction",
            "Creative Director",
            "Creative criteria stay explicit and revisions remain bounded.",
            ["general.task"],
            reports_to="owner",
        ),
        _position(
            "trend-researcher",
            "trend-lab",
            "Trend Researcher",
            "Current authorized evidence becomes abstract, original creative guidance.",
            ["general.task"],
            reports_to="creative-director",
            tools=["music.trends.read"],
        ),
        _position(
            "lyricist",
            "songwriting",
            "Lyricist",
            "Versioned original lyrics satisfy the current brief.",
            ["general.task"],
            reports_to="creative-director",
        ),
        _position(
            "music-producer",
            "production",
            "Music Producer",
            "A provider-neutral composition plan tests explicit creative hypotheses.",
            ["general.task"],
            reports_to="creative-director",
        ),
        _position(
            "generation-operator",
            "production",
            "Generation Operator",
            "Generation jobs are bounded, traceable, and safely imported.",
            ["general.task"],
            reports_to="music-producer",
            tools=["music.generate", "music.generation.read", "music.audio.import"],
        ),
        _position(
            "audio-critic",
            "listening",
            "Audio Critic",
            "Every recommendation cites actual audio-derived evidence.",
            ["general.task"],
            reports_to="creative-director",
            tools=["music.audio.analyze"],
        ),
        _object_type(
            "music-project",
            "Music Project",
            {
                "title": {"type": "string", "minLength": 1},
                "audience": {"type": "string", "minLength": 1},
                "language": {"type": "string", "minLength": 2},
                "mood": {"type": "string", "minLength": 1},
                "themes": {"type": "array", "items": {"type": "string"}},
                "genre_attributes": {"type": "array", "items": {"type": "string"}},
                "use_plan": {"type": "string", "enum": list(USE_PLANS)},
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 5},
                "task_id": {"type": "string"},
            },
            [
                "title",
                "audience",
                "language",
                "mood",
                "themes",
                "genre_attributes",
                "use_plan",
                "max_rounds",
                "task_id",
            ],
            review_position="creative-director",
        ),
        _object_type(
            "trend-dossier",
            "Trend Dossier",
            {
                "project_id": {"type": "string"},
                "observed_at": {"type": "string", "format": "date-time"},
                "attributes": {"type": "array", "items": {"type": "string"}},
                "limitations": {"type": "string"},
            },
            ["project_id", "observed_at", "attributes", "limitations"],
            review_position="creative-director",
        ),
        _object_type(
            "lyrics-draft",
            "Lyrics Draft",
            {
                "project_id": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
                "language": {"type": "string"},
                "lyrics_artifact_id": {"type": "string"},
                "revision_reason": {"type": "string"},
            },
            ["project_id", "version", "language", "lyrics_artifact_id", "revision_reason"],
            review_position="creative-director",
        ),
        _object_type(
            "composition-spec",
            "Composition Spec",
            {
                "project_id": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
                "tempo_range": {"type": "string"},
                "arrangement": {"type": "array", "items": {"type": "string"}},
                "song_form": {"type": "array", "items": {"type": "string"}},
            },
            ["project_id", "version", "tempo_range", "arrangement", "song_form"],
            review_position="creative-director",
        ),
        _object_type(
            "audio-candidate",
            "Audio Candidate",
            {
                "project_id": {"type": "string"},
                "round": {"type": "integer", "minimum": 1, "maximum": 5},
                "variant": {"type": "string"},
                "audio_artifact_id": {"type": "string"},
                "audio_version_id": {"type": "string"},
                "audio_digest": {"type": "string"},
                "provider": {"type": "string"},
            },
            [
                "project_id",
                "round",
                "variant",
                "audio_artifact_id",
                "audio_version_id",
                "audio_digest",
                "provider",
            ],
            review_position="audio-critic",
        ),
        _object_type(
            "listening-review",
            "Listening Review",
            {
                "project_id": {"type": "string"},
                "candidate_id": {"type": "string"},
                "overall_score": {"type": "integer", "minimum": 0, "maximum": 100},
                "evidence_artifact_id": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "string"}},
                "decision": {"type": "string", "enum": ["SHORTLIST", "REVISE", "REJECT"]},
            },
            [
                "project_id",
                "candidate_id",
                "overall_score",
                "evidence_artifact_id",
                "findings",
                "decision",
            ],
            review_position="creative-director",
        ),
        _object_type(
            "revision-request",
            "Revision Request",
            {
                "project_id": {"type": "string"},
                "round": {"type": "integer", "minimum": 2, "maximum": 5},
                "failed_criterion": {"type": "string", "minLength": 1},
                "requested_change": {"type": "string", "minLength": 1},
                "requested_by": {"type": "string", "minLength": 1},
                "remaining_rounds": {"type": "integer", "minimum": 0, "maximum": 4},
            },
            [
                "project_id",
                "round",
                "failed_criterion",
                "requested_change",
                "requested_by",
                "remaining_rounds",
            ],
            review_position="owner",
        ),
        _object_type(
            "final-release-package",
            "Final Release Package",
            {
                "project_id": {"type": "string"},
                "candidate_id": {"type": "string"},
                "audio_artifact_id": {"type": "string"},
                "audio_version_id": {"type": "string"},
                "lyrics_artifact_id": {"type": "string"},
                "review_id": {"type": "string"},
                "rights_manifest_artifact_id": {"type": "string"},
                "package_artifact_id": {"type": "string"},
                "package_version_id": {"type": "string"},
                "current_round": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            [
                "project_id",
                "candidate_id",
                "audio_artifact_id",
                "audio_version_id",
                "lyrics_artifact_id",
                "review_id",
                "rights_manifest_artifact_id",
                "current_round",
            ],
            review_position="owner",
            schema_version=2,
            extra_actions={
                "select_candidate": {
                    "from": ["IN_REVIEW"],
                    "to": "IN_REVIEW",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "audio_artifact_id": {"type": "string"},
                            "audio_version_id": {"type": "string"},
                            "review_id": {"type": "string"},
                        },
                        "required": [
                            "candidate_id",
                            "audio_artifact_id",
                            "audio_version_id",
                            "review_id",
                        ],
                        "additionalProperties": False,
                    },
                    "allowed_update_fields": [
                        "candidate_id",
                        "audio_artifact_id",
                        "audio_version_id",
                        "review_id",
                    ],
                    "required_evidence": True,
                    "required_position_keys": ["owner"],
                },
                "attach_package": {
                    "from": ["IN_REVIEW"],
                    "to": "IN_REVIEW",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "package_artifact_id": {"type": "string"},
                            "package_version_id": {"type": "string"},
                        },
                        "required": ["package_artifact_id", "package_version_id"],
                        "additionalProperties": False,
                    },
                    "allowed_update_fields": [
                        "package_artifact_id",
                        "package_version_id",
                    ],
                    "required_evidence": True,
                    "required_position_keys": ["owner"],
                },
                "request_revision": {
                    "from": ["IN_REVIEW"],
                    "to": "IN_REVIEW",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "audio_artifact_id": {"type": "string"},
                            "audio_version_id": {"type": "string"},
                            "lyrics_artifact_id": {"type": "string"},
                            "review_id": {"type": "string"},
                            "current_round": {
                                "type": "integer",
                                "minimum": 2,
                                "maximum": 5,
                            },
                        },
                        "required": [
                            "candidate_id",
                            "audio_artifact_id",
                            "audio_version_id",
                            "lyrics_artifact_id",
                            "review_id",
                            "current_round",
                        ],
                        "additionalProperties": False,
                    },
                    "allowed_update_fields": [
                        "candidate_id",
                        "audio_artifact_id",
                        "audio_version_id",
                        "lyrics_artifact_id",
                        "review_id",
                        "current_round",
                    ],
                    "required_evidence": True,
                    "required_position_keys": ["owner"],
                }
            },
        ),
    ]
    return {
        "template": {
            "slug": TEMPLATE_SLUG,
            "mission": DEFAULT_MISSION,
            "configuration_fields": ["default_language", "default_genre", "use_plan"],
            "safety": {
                "external_writes_enabled": False,
                "artist_imitation_enabled": False,
                "voice_cloning_enabled": False,
                "distribution_enabled": False,
            },
        },
        "resources": deepcopy(resources),
    }


def _configuration(values: dict[str, Any]) -> dict[str, Any]:
    language = str(values.get("default_language", "")).strip()
    if not 2 <= len(language) <= 32:
        raise InvalidCompanyPack("Default language must contain 2 to 32 characters")
    genre = str(values.get("default_genre", "")).strip()
    if not 1 <= len(genre) <= 120:
        raise InvalidCompanyPack("Default genre must contain 1 to 120 characters")
    use_plan = str(values.get("use_plan", "")).strip().lower()
    if use_plan not in USE_PLANS:
        raise InvalidCompanyPack("Use plan must be one of: " + ", ".join(USE_PLANS))
    return {
        "default_language": language,
        "default_genre": genre,
        "use_plan": use_plan,
        "generation_provider": "deterministic-demo",
        "external_writes_enabled": False,
    }


DEFINITION = CompanyTemplateDefinition(
    slug=TEMPLATE_SLUG,
    key=PACK_KEY,
    version=PACK_VERSION,
    name=PACK_NAME,
    mission=DEFAULT_MISSION,
    manifest_factory=manifest,
    configuration_factory=_configuration,
    required_features=("business_objects", "company_model"),
)


def build_pack() -> CompanyPack:
    return DEFINITION.build_pack()
