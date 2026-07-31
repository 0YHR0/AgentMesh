from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.business_object_services import BusinessObjectService
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.domain.artifacts import ArtifactClassification
from agentmesh.domain.business_objects import ObjectSourceType
from agentmesh.domain.errors import InvalidCompanyPack, TaskNotFound
from agentmesh.domain.tasks import TaskStatus
from agentmesh.domain.tools import ToolInvocationStatus

WORKFLOW_KEY = "live-market-research"
MAX_SOURCES = 50
MAX_CLAIMS = 100


@dataclass(frozen=True)
class ResearchMaterialization:
    task_id: UUID
    status: str
    source_record_ids: list[UUID]
    claim_register_ids: list[UUID]
    report_id: UUID | None
    artifact_id: UUID | None
    message: str | None = None


class ResearchMaterializationService:
    """Turn a completed governed research Task into durable draft business records."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        business_object_service: BusinessObjectService,
        artifact_service: ArtifactService,
        tenant_id: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._business_objects = business_object_service
        self._artifacts = artifact_service
        self._tenant_id = tenant_id

    def materialize_if_ready(
        self, task_id: UUID, *, actor: str
    ) -> ResearchMaterialization | None:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if (
                task is None
                or task.tenant_id != self._tenant_id
                or task.input.get("workflow") != WORKFLOW_KEY
                or task.status is not TaskStatus.COMPLETED
            ):
                return None
        return self.materialize(task_id, actor=actor)

    def status(self, task_id: UUID) -> ResearchMaterialization:
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            if task is None or task.tenant_id != self._tenant_id:
                raise TaskNotFound(task_id)
            if task.input.get("workflow") != WORKFLOW_KEY:
                raise InvalidCompanyPack("Task is not a live market-research workflow")
            company_id = self._company_id(task.input)
            report_type = uow.business_objects.get_type_by_key(
                company_id, "research-report", published_only=True
            )
            report = (
                uow.business_objects.get_object_by_external_ref(
                    report_type.id, self._report_ref(task_id)
                )
                if report_type is not None
                else None
            )
            if report is None:
                state = "READY" if task.status is TaskStatus.COMPLETED else "WAITING"
                return ResearchMaterialization(
                    task_id, state, [], [], None, None,
                    "Task output has not been materialized yet.",
                )
            revision = uow.business_objects.get_revision(
                report.id, report.current_revision
            )
            data = revision.data if revision is not None else {}
            claim_ids = self._uuid_list(data.get("claim_register_ids"))
            source_ids: list[UUID] = []
            for claim_id in claim_ids:
                claim = uow.business_objects.get_object(claim_id)
                if claim is None or claim.company_id != company_id:
                    continue
                claim_revision = uow.business_objects.get_revision(
                    claim.id, claim.current_revision
                )
                if claim_revision is not None:
                    source_ids.extend(
                        self._uuid_list(claim_revision.data.get("source_record_ids"))
                    )
            return ResearchMaterialization(
                task_id=task_id,
                status="MATERIALIZED",
                source_record_ids=list(dict.fromkeys(source_ids)),
                claim_register_ids=claim_ids,
                report_id=report.id,
                artifact_id=self._optional_uuid(data.get("artifact_id")),
            )

    def materialize(self, task_id: UUID, *, actor: str) -> ResearchMaterialization:
        current = self.status(task_id)
        if current.status == "MATERIALIZED":
            return current
        with self._uow_factory() as uow:
            task = uow.tasks.get(task_id)
            assert task is not None
            if task.status is not TaskStatus.COMPLETED:
                raise InvalidCompanyPack("Research Task must be completed before materialization")
            company_id = self._company_id(task.input)
            subtasks = {value.key: value for value in uow.subtasks.list_for_task(task.id)}
            report_subtask = subtasks.get("report-draft")
            raw = (
                report_subtask.output.get("research_deliverable")
                if report_subtask is not None and report_subtask.output is not None
                else None
            )
            invocations = {
                str(value.id): value
                for value in uow.tool_invocations.list_for_task(task.id)
                if value.status is ToolInvocationStatus.SUCCEEDED
                and value.result_digest is not None
            }
            positions = {
                key: uow.company_model.get_position_by_key(company_id, key)
                for key in ("research-specialist", "fact-reviewer", "editorial-reviewer")
            }
            types = {
                key: uow.business_objects.get_type_by_key(
                    company_id, key, published_only=True
                )
                for key in ("source-record", "claim-register", "research-report")
            }
            producer_run_id = report_subtask.current_run_id if report_subtask else None
            expected_audience = str(task.input.get("target_audience", "")).strip()
            requested_max_sources = task.input.get("max_sources", MAX_SOURCES)
        if (
            not expected_audience
            or not isinstance(requested_max_sources, int)
            or isinstance(requested_max_sources, bool)
        ):
            raise InvalidCompanyPack("Research Task materialization context is invalid")
        bundle = self._validate_bundle(
            raw,
            invocations,
            expected_audience=expected_audience,
            max_sources=min(requested_max_sources, MAX_SOURCES),
        )
        if any(value is None for value in positions.values()) or any(
            value is None for value in types.values()
        ):
            raise InvalidCompanyPack("Research materialization resources are unavailable")

        source_ids: list[UUID] = []
        source_ids_by_uri: dict[str, UUID] = {}
        for source in bundle["sources"]:
            external_ref = self._source_ref(task_id, source["uri"])
            snapshot = self._get_or_create_object(
                company_id=company_id,
                type_id=types["source-record"].id,
                external_ref=external_ref,
                data={key: source[key] for key in (
                    "title", "uri", "publisher", "retrieved_at", "excerpt_digest"
                )},
                actor=actor,
                owner_position_id=positions["research-specialist"].id,
                source_id=str(task_id),
                evidence_refs=[
                    self._invocation_evidence(invocations[value])
                    for value in source["tool_invocation_ids"]
                ],
            )
            source_ids.append(snapshot.object.id)
            source_ids_by_uri[source["uri"]] = snapshot.object.id

        claim_ids: list[UUID] = []
        for index, claim in enumerate(bundle["claims"]):
            linked = [source_ids_by_uri[value] for value in claim["source_uris"]]
            snapshot = self._get_or_create_object(
                company_id=company_id,
                type_id=types["claim-register"].id,
                external_ref=self._claim_ref(task_id, index, claim["claim"]),
                data={
                    "claim": claim["claim"],
                    "source_record_ids": [str(value) for value in linked],
                    "confidence": claim["confidence"],
                    "limitations": claim["limitations"],
                },
                actor=actor,
                owner_position_id=positions["fact-reviewer"].id,
                source_id=str(task_id),
                evidence_refs=[f"business-object:{value}" for value in linked],
            )
            claim_ids.append(snapshot.object.id)

        report = bundle["report"]
        artifact = self._artifacts.create_artifact(
            display_name=report["title"],
            kind="research-report",
            classification=ArtifactClassification.INTERNAL,
            media_type="text/plain",
            content=report["markdown"].encode(),
            producer_run_id=producer_run_id,
            idempotency_key=f"research-task:{task_id}:report",
        )
        report_snapshot = self._get_or_create_object(
            company_id=company_id,
            type_id=types["research-report"].id,
            external_ref=self._report_ref(task_id),
            data={
                "title": report["title"],
                "audience": report["audience"],
                "claim_register_ids": [str(value) for value in claim_ids],
                "artifact_id": str(artifact.artifact.id),
            },
            actor=actor,
            owner_position_id=positions["editorial-reviewer"].id,
            source_id=str(task_id),
            evidence_refs=[
                f"business-object:{value}" for value in claim_ids
            ] + [f"artifact:{artifact.artifact.id}:version:{artifact.versions[0].id}"],
        )
        return ResearchMaterialization(
            task_id=task_id,
            status="MATERIALIZED",
            source_record_ids=source_ids,
            claim_register_ids=claim_ids,
            report_id=report_snapshot.object.id,
            artifact_id=artifact.artifact.id,
        )

    def _get_or_create_object(self, **values: Any):
        with self._uow_factory() as uow:
            existing = uow.business_objects.get_object_by_external_ref(
                values["type_id"], values["external_ref"]
            )
        if existing is not None:
            return self._business_objects.get_object(values["company_id"], existing.id)
        return self._business_objects.create_object(
            values["company_id"],
            type_id=values["type_id"],
            data=values["data"],
            actor=values["actor"],
            source_type=ObjectSourceType.AGENT,
            source_id=values["source_id"],
            external_ref=values["external_ref"],
            owner_position_id=values["owner_position_id"],
            evidence_refs=values["evidence_refs"],
        )

    @staticmethod
    def _validate_bundle(
        raw: Any,
        invocations: dict[str, Any],
        *,
        expected_audience: str,
        max_sources: int,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise InvalidCompanyPack("report-draft did not return research_deliverable")
        sources = raw.get("sources")
        claims = raw.get("claims")
        report = raw.get("report")
        if not isinstance(sources, list) or not 1 <= len(sources) <= max_sources:
            raise InvalidCompanyPack(
                f"research_deliverable sources must contain 1-{max_sources} records"
            )
        if not isinstance(claims, list) or not 1 <= len(claims) <= MAX_CLAIMS:
            raise InvalidCompanyPack("research_deliverable claims must contain 1-100 records")
        if not isinstance(report, dict):
            raise InvalidCompanyPack("research_deliverable report is required")
        normalized_sources: list[dict[str, Any]] = []
        uris: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                raise InvalidCompanyPack("Every source must be an object")
            title = ResearchMaterializationService._text(source, "title", 255)
            uri = ResearchMaterializationService._text(source, "uri", 2_048)
            parsed = urlparse(uri)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or uri in uris:
                raise InvalidCompanyPack("Source URIs must be unique absolute http(s) URIs")
            publisher = ResearchMaterializationService._text(source, "publisher", 255)
            retrieved_at = ResearchMaterializationService._text(source, "retrieved_at", 64)
            try:
                timestamp = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidCompanyPack("Source retrieved_at must be ISO-8601") from exc
            if timestamp.tzinfo is None:
                raise InvalidCompanyPack("Source retrieved_at must include a timezone")
            digest = ResearchMaterializationService._text(source, "excerpt_digest", 71)
            if not ResearchMaterializationService._valid_digest(digest):
                raise InvalidCompanyPack("Source excerpt_digest must be sha256:<64 lowercase hex>")
            invocation_ids = source.get("tool_invocation_ids")
            if not isinstance(invocation_ids, list) or not invocation_ids:
                raise InvalidCompanyPack("Every source must cite a Tool Invocation")
            if any(
                not isinstance(value, str) or value not in invocations
                for value in invocation_ids
            ):
                raise InvalidCompanyPack("Source cites an unknown or unsuccessful Tool Invocation")
            uris.add(uri)
            normalized_sources.append({
                "title": title, "uri": uri, "publisher": publisher,
                "retrieved_at": timestamp.isoformat(), "excerpt_digest": digest,
                "tool_invocation_ids": sorted(set(invocation_ids)),
            })
        normalized_claims: list[dict[str, Any]] = []
        for claim in claims:
            if not isinstance(claim, dict):
                raise InvalidCompanyPack("Every claim must be an object")
            source_uris = claim.get("source_uris")
            if not isinstance(source_uris, list) or not source_uris or any(
                not isinstance(value, str) or value not in uris for value in source_uris
            ):
                raise InvalidCompanyPack("Every claim must cite sources in the same bundle")
            confidence = claim.get("confidence")
            if confidence not in {"LOW", "MEDIUM", "HIGH"}:
                raise InvalidCompanyPack("Claim confidence must be LOW, MEDIUM, or HIGH")
            normalized_claims.append({
                "claim": ResearchMaterializationService._text(claim, "claim", 4_000),
                "source_uris": list(dict.fromkeys(source_uris)),
                "confidence": confidence,
                "limitations": ResearchMaterializationService._text(
                    claim, "limitations", 4_000
                ),
            })
        audience = ResearchMaterializationService._text(report, "audience", 500)
        if audience != expected_audience:
            raise InvalidCompanyPack("Report audience does not match the Research Task contract")
        return {
            "sources": normalized_sources,
            "claims": normalized_claims,
            "report": {
                "title": ResearchMaterializationService._text(report, "title", 255),
                "audience": audience,
                "markdown": ResearchMaterializationService._text(report, "markdown", 60_000),
            },
        }

    @staticmethod
    def _text(value: dict[str, Any], key: str, maximum: int) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > maximum:
            raise InvalidCompanyPack(f"research_deliverable {key} is invalid")
        return item.strip()

    @staticmethod
    def _valid_digest(value: str) -> bool:
        if not value.startswith("sha256:") or len(value) != 71:
            return False
        try:
            int(value[7:], 16)
        except ValueError:
            return False
        return value[7:] == value[7:].lower()

    @staticmethod
    def _company_id(task_input: dict[str, Any]) -> UUID:
        try:
            return UUID(str(task_input["company_id"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise InvalidCompanyPack("Research Task has no valid company_id") from exc

    @staticmethod
    def _source_ref(task_id: UUID, uri: str) -> str:
        return f"research-task:{task_id}:source:{sha256(uri.encode()).hexdigest()}"

    @staticmethod
    def _claim_ref(task_id: UUID, index: int, claim: str) -> str:
        digest = sha256(claim.encode()).hexdigest()
        return f"research-task:{task_id}:claim:{index}:{digest}"

    @staticmethod
    def _report_ref(task_id: UUID) -> str:
        return f"research-task:{task_id}:report"

    @staticmethod
    def _invocation_evidence(invocation: Any) -> str:
        return f"mcp-invocation:{invocation.id}:result:{invocation.result_digest}"

    @staticmethod
    def _uuid_list(value: Any) -> list[UUID]:
        if not isinstance(value, list):
            return []
        result: list[UUID] = []
        for item in value:
            try:
                result.append(UUID(str(item)))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        try:
            return UUID(str(value)) if value is not None else None
        except (TypeError, ValueError):
            return None
