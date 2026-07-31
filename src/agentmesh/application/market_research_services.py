from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from agentmesh.application.business_object_services import (
    BusinessObjectService,
    BusinessObjectSnapshot,
)
from agentmesh.application.mcp_registry_services import McpRegistryService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.credentials import CredentialBindingStatus, SecretReferenceStatus
from agentmesh.domain.errors import AgentMeshError, CompanyPackConflict, InvalidCompanyPack
from agentmesh.domain.model_runtime import AgentToolPolicy, ModelRuntimePolicy
from agentmesh.domain.registry import (
    AgentDefinitionLifecycle,
    AgentVersionStatus,
)
from agentmesh.domain.tasks import TaskAggregate, TaskExecutionMode, utc_now
from agentmesh.domain.tools import ToolSideEffect
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.templates.market_intelligence_studio import PACK_KEY

REQUIRED_TOOL_KEYS = ("web.search", "source.read")
RESEARCH_POSITION_KEYS = (
    "research-lead",
    "research-specialist",
    "fact-reviewer",
    "editorial-reviewer",
)


@dataclass(frozen=True)
class MarketResearchPreflight:
    company_id: UUID | None
    ready: bool
    blockers: list[dict[str, str]]
    warnings: list[dict[str, str]]
    tools: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    output_contract: list[str]
    external_writes_enabled: bool = False


@dataclass(frozen=True)
class MarketResearchLaunch:
    task: TaskAggregate
    research_question: BusinessObjectSnapshot
    preflight: MarketResearchPreflight


class MarketResearchService:
    """Launch the first provider-neutral, governed market-research workflow."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        task_service: TaskApplicationService,
        business_object_service: BusinessObjectService,
        mcp_registry_service: McpRegistryService,
        tenant_id: str,
        feature_gates: FeatureGateSet,
        credential_workload_principal_id: UUID | None = None,
        environment: str = "development",
    ) -> None:
        self._uow_factory = uow_factory
        self._task_service = task_service
        self._business_objects = business_object_service
        self._mcp_registry = mcp_registry_service
        self._tenant_id = tenant_id
        self._feature_gates = feature_gates
        self._credential_workload_principal_id = credential_workload_principal_id
        self._environment = environment.strip().lower()

    def preflight(self) -> MarketResearchPreflight:
        blockers: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        required_features = (
            Feature.COMPANY_PACKS,
            Feature.BUSINESS_OBJECTS,
            Feature.AGENT_REGISTRY_MANAGEMENT,
            Feature.COORDINATED_EXECUTION,
            Feature.MCP_READ_TOOLS,
            Feature.GOVERNED_MCP,
        )
        for feature in required_features:
            if not self._feature_gates.is_enabled(feature):
                blockers.append(
                    {
                        "code": "feature_disabled",
                        "subject": feature.value,
                        "message": f"Enable the '{feature.value}' feature.",
                    }
                )

        tools: list[dict[str, Any]] = []
        for logical_key in REQUIRED_TOOL_KEYS:
            try:
                binding = self._mcp_registry.resolve(logical_key)
            except AgentMeshError as exc:
                blockers.append(
                    {
                        "code": "tool_unavailable",
                        "subject": logical_key,
                        "message": str(exc),
                    }
                )
                tools.append({"logical_key": logical_key, "ready": False})
                continue
            read_only = binding.side_effect is ToolSideEffect.READ_ONLY
            if not read_only:
                blockers.append(
                    {
                        "code": "tool_not_read_only",
                        "subject": logical_key,
                        "message": "Live research only accepts read-only MCP bindings.",
                    }
                )
            if binding.authentication_required:
                credential_ready = False
                if (
                    self._feature_gates.is_enabled(Feature.CREDENTIAL_BROKER)
                    and self._credential_workload_principal_id is not None
                    and binding.server_version_id is not None
                ):
                    with self._uow_factory() as uow:
                        credential = uow.credentials.find_mcp_binding(
                            tenant_id=self._tenant_id,
                            workload_principal_id=self._credential_workload_principal_id,
                            server_version_id=binding.server_version_id,
                            environment=self._environment,
                        )
                        reference = (
                            uow.credentials.get_secret_reference(
                                credential.secret_reference_id
                            )
                            if credential is not None
                            else None
                        )
                    credential_ready = bool(
                        credential
                        and credential.status is CredentialBindingStatus.ACTIVE
                        and credential.expires_at > utc_now()
                        and credential.configuration_digest == binding.configuration_digest
                        and reference
                        and reference.status is SecretReferenceStatus.ACTIVE
                    )
                if not credential_ready:
                    blockers.append(
                        {
                            "code": "credential_binding_unready",
                            "subject": logical_key,
                            "message": (
                                "Configure an active workload-bound Credential Broker binding "
                                "for this MCP Server Version."
                            ),
                        }
                    )
                else:
                    warnings.append(
                        {
                            "code": "credential_isolated",
                            "subject": logical_key,
                            "message": "Authentication is isolated behind the Credential Broker.",
                        }
                    )
            tools.append(
                {
                    "logical_key": logical_key,
                    "server_name": binding.server_name,
                    "tool_name": binding.tool_name,
                    "transport": binding.transport,
                    "authentication_required": binding.authentication_required,
                    "ready": read_only and (
                        not binding.authentication_required or credential_ready
                    ),
                }
            )

        company_id: UUID | None = None
        positions: list[dict[str, Any]] = []
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            if company is None:
                blockers.append(
                    {
                        "code": "company_missing",
                        "subject": "market-intelligence-studio",
                        "message": "Install the Market Intelligence Studio template first.",
                    }
                )
            else:
                company_id = company.id
                if uow.company_packs.get_installation(company.id, PACK_KEY) is None:
                    blockers.append(
                        {
                            "code": "template_missing",
                            "subject": PACK_KEY,
                            "message": "The active Company is not a Market Intelligence Studio.",
                        }
                    )
                question_type = uow.business_objects.get_type_by_key(
                    company.id, "research-question", published_only=True
                )
                if question_type is None:
                    blockers.append(
                        {
                            "code": "business_object_type_missing",
                            "subject": "research-question",
                            "message": "The published Research Question type is unavailable.",
                        }
                    )
                for position_key in RESEARCH_POSITION_KEYS:
                    position = uow.company_model.get_position_by_key(company.id, position_key)
                    appointment = (
                        uow.company_model.get_active_appointment(position.id)
                        if position is not None
                        else None
                    )
                    version = (
                        uow.agent_versions.get(appointment.agent_version_id)
                        if appointment is not None
                        else None
                    )
                    definition = (
                        uow.agent_definitions.get(appointment.agent_definition_id)
                        if appointment is not None
                        else None
                    )
                    base_ready = bool(
                        position
                        and appointment
                        and version
                        and definition
                        and definition.tenant_id == self._tenant_id
                        and definition.lifecycle is AgentDefinitionLifecycle.ACTIVE
                        and definition.default_version_id == version.id
                        and version.status is AgentVersionStatus.PUBLISHED
                        and version.content_digest
                        and "async" in version.execution_modes
                        and set(position.required_capabilities).issubset(
                            version.verified_capabilities
                        )
                    )
                    required_tools = (
                        REQUIRED_TOOL_KEYS
                        if position_key in {"research-lead", "research-specialist"}
                        else ()
                    )
                    allowed_tools = (
                        AgentToolPolicy.from_dict(version.tool_profile).allowed_tools
                        if version is not None
                        else ()
                    )
                    missing_tools = sorted(set(required_tools) - set(allowed_tools))
                    ready = base_ready and not missing_tools
                    if not ready:
                        message = (
                            "Appoint an active, published, async-capable Agent Version."
                            if base_ready is False
                            else "The appointed Agent Version must allow: "
                            + ", ".join(missing_tools)
                        )
                        blockers.append(
                            {
                                "code": (
                                    "appointment_unready"
                                    if base_ready is False
                                    else "agent_tools_missing"
                                ),
                                "subject": position_key,
                                "message": message,
                            }
                        )
                    provider = (
                        ModelRuntimePolicy.from_dict(version.model_policy).provider
                        if version is not None
                        else None
                    )
                    if ready and provider == "deterministic":
                        warnings.append(
                            {
                                "code": "deterministic_agent",
                                "subject": position_key,
                                "message": (
                                    "This appointment will run deterministically rather than use "
                                    "a live model provider."
                                ),
                            }
                        )
                    positions.append(
                        {
                            "key": position_key,
                            "title": position.title if position else position_key,
                            "appointment_id": str(appointment.id) if appointment else None,
                            "agent_name": definition.name if definition else None,
                            "agent_version_id": str(version.id) if version else None,
                            "model_provider": provider,
                            "required_tools": list(required_tools),
                            "missing_tools": missing_tools,
                            "ready": ready,
                        }
                    )

        return MarketResearchPreflight(
            company_id=company_id,
            ready=not blockers,
            blockers=blockers,
            warnings=warnings,
            tools=tools,
            positions=positions,
            output_contract=[
                "Attributable source records with retrieval metadata",
                "Claims linked to source records with confidence and limitations",
                "An internally reviewed research-report draft",
                "No external publication or customer delivery",
            ],
        )

    def launch(
        self,
        *,
        question: str,
        target_audience: str,
        decision_supported: str,
        scope: str,
        max_sources: int,
        requested_by: str,
        idempotency_key: str,
    ) -> MarketResearchLaunch:
        preflight = self.preflight()
        if not preflight.ready or preflight.company_id is None:
            codes = ", ".join(value["subject"] for value in preflight.blockers)
            raise CompanyPackConflict(f"Live research preflight failed: {codes}")
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise InvalidCompanyPack("Live research requires an Idempotency-Key")
        normalized_question = question.strip()
        normalized_audience = target_audience.strip()
        normalized_decision = decision_supported.strip()
        if len(normalized_question) < 10:
            raise InvalidCompanyPack("Research question must contain at least 10 characters")
        if not normalized_audience or not normalized_decision:
            raise InvalidCompanyPack("Target audience and supported decision are required")
        if not 3 <= max_sources <= 50:
            raise InvalidCompanyPack("Research max_sources must be between 3 and 50")
        normalized_scope = scope.strip()
        if not normalized_scope:
            raise InvalidCompanyPack("Research scope must not be blank")
        key_digest = sha256(normalized_key.encode()).hexdigest()

        positions = {value["key"]: value for value in preflight.positions}
        company_id = preflight.company_id
        with self._uow_factory() as uow:
            question_type = uow.business_objects.get_type_by_key(
                company_id, "research-question", published_only=True
            )
            lead = uow.company_model.get_position_by_key(company_id, "research-lead")
            assert question_type is not None and lead is not None
            external_ref = f"research-engagement:{key_digest}"
            existing = uow.business_objects.get_object_by_external_ref(
                question_type.id, external_ref
            )
        if existing is None:
            research_question = self._business_objects.create_object(
                company_id,
                type_id=question_type.id,
                data={
                    "question": normalized_question,
                    "target_audience": normalized_audience,
                    "decision_supported": normalized_decision,
                },
                actor=requested_by,
                external_ref=external_ref,
                owner_position_id=lead.id,
            )
        else:
            research_question = self._business_objects.get_object(company_id, existing.id)

        context = {
            "company_id": str(company_id),
            "research_question_id": str(research_question.object.id),
            "question": normalized_question,
            "target_audience": normalized_audience,
            "decision_supported": normalized_decision,
            "scope": normalized_scope,
            "max_sources": max_sources,
            "required_tools": list(REQUIRED_TOOL_KEYS),
            "evidence_contract": {
                "source_records": "Attributable URI, publisher, retrieval time and excerpt digest",
                "claims": "Each material claim cites source record IDs and states limitations",
                "report": "Internal draft only; external publication is prohibited",
            },
            "materialization_contract": {
                "response": "Return JSON with summary and research_deliverable.",
                "research_deliverable": {
                    "sources": [
                        {
                            "title": "string",
                            "uri": "absolute http(s) URI",
                            "publisher": "string",
                            "retrieved_at": "timezone-aware ISO-8601 date-time",
                            "excerpt_digest": "sha256:<64 lowercase hex characters>",
                            "tool_invocation_ids": ["AgentMesh MCP Tool Invocation UUID"],
                        }
                    ],
                    "claims": [
                        {
                            "claim": "string",
                            "source_uris": ["URI present in sources"],
                            "confidence": "LOW | MEDIUM | HIGH",
                            "limitations": "string",
                        }
                    ],
                    "report": {
                        "title": "string",
                        "audience": normalized_audience,
                        "markdown": "decision-ready internal report with citations",
                    },
                },
                "rule": (
                    "Carry the complete bundle forward at every stage; report-draft must "
                    "return the final validated bundle. Never invent Tool Invocation IDs."
                ),
            },
        }

        def spec(
            key: str,
            objective: str,
            position_key: str,
            *,
            depends_on: tuple[str, ...] = (),
        ) -> SubtaskSpec:
            position = positions[position_key]
            return SubtaskSpec.create(
                key=key,
                objective=objective,
                input={**context, "position_key": position_key},
                required_capabilities=(
                    {
                        "scope-plan": ("research.plan",),
                        "evidence-collection": ("research.collect", "source.verify"),
                        "claim-synthesis": ("evidence.review",),
                        "fact-check": ("evidence.audit", "claim.review"),
                        "report-draft": ("editorial.review", "content.quality"),
                    }[key]
                ),
                depends_on=depends_on,
                preferred_agent_id=position["agent_name"],
            )

        plan = CoordinatedPlan.create(
            (
                spec(
                    "scope-plan",
                    "Turn the question into a bounded research plan and source strategy.",
                    "research-lead",
                ),
                spec(
                    "evidence-collection",
                    "Use governed read-only tools to collect attributable source evidence.",
                    "research-specialist",
                    depends_on=("scope-plan",),
                ),
                spec(
                    "claim-synthesis",
                    "Synthesize evidence into claims with confidence and explicit limitations.",
                    "research-lead",
                    depends_on=("evidence-collection",),
                ),
                spec(
                    "fact-check",
                    "Audit every material claim against the source evidence contract.",
                    "fact-reviewer",
                    depends_on=("claim-synthesis",),
                ),
                spec(
                    "report-draft",
                    (
                        "Produce an internally reviewed decision-ready report draft; do not "
                        "publish. Return the complete materialization_contract JSON bundle."
                    ),
                    "editorial-reviewer",
                    depends_on=("fact-check",),
                ),
            ),
            max_concurrency=2,
        )
        aggregate = self._task_service.create_task(
            objective=f"Research: {normalized_question}",
            project_id="market-intelligence",
            input={"workflow": "live-market-research", **context},
            execution_mode=TaskExecutionMode.COORDINATED,
            coordinated_plan=plan,
            goal_constraints=(
                "Use only governed read-only MCP tools",
                "Keep every material claim traceable to attributable evidence",
                "Do not publish or deliver externally",
            ),
            goal_success_criteria=(
                "Produce source records and a claim register",
                "Produce one internally reviewed research-report draft",
            ),
            idempotency_key=f"live-market-research:{key_digest}",
        )
        started = self._task_service.request_run(
            aggregate.task.id,
            idempotency_key=f"live-market-research-run:{key_digest}",
        )
        return MarketResearchLaunch(
            task=started,
            research_question=research_question,
            preflight=preflight,
        )
