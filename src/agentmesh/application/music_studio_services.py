from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.application.business_object_services import (
    BusinessObjectService,
    BusinessObjectSnapshot,
)
from agentmesh.application.ports import UnitOfWorkFactory
from agentmesh.application.registry_services import AgentRegistryService
from agentmesh.application.services import TaskApplicationService
from agentmesh.domain.artifacts import ArtifactClassification
from agentmesh.domain.business_objects import ObjectSourceType
from agentmesh.domain.company import Appointment
from agentmesh.domain.coordination import CoordinatedPlan, SubtaskSpec
from agentmesh.domain.errors import InvalidCompanyPack
from agentmesh.domain.tasks import TaskAggregate, TaskExecutionMode, TaskStatus
from agentmesh.integrations.music.deterministic import (
    DeterministicAudioAnalyzer,
    DeterministicMusicProvider,
)
from agentmesh.templates.music_studio import PACK_KEY

WORKFLOW_KEY = "music-studio-demo"
AGENTS = {
    "brief": ("music-creative-director", "creative-director", "Creative Director"),
    "trend": ("music-trend-researcher", "trend-researcher", "Trend Researcher"),
    "lyrics": ("music-lyricist", "lyricist", "Lyricist"),
    "production": ("music-producer", "music-producer", "Music Producer"),
    "generation": ("music-generation-operator", "generation-operator", "Generation Operator"),
    "listening": ("music-audio-critic", "audio-critic", "Audio Critic"),
}


@dataclass(frozen=True)
class MusicProjectLaunch:
    task: TaskAggregate
    project: BusinessObjectSnapshot


@dataclass(frozen=True)
class MusicProjectResult:
    task_id: UUID
    status: str
    project_id: UUID
    candidate_id: UUID | None = None
    review_id: UUID | None = None
    release_id: UUID | None = None
    audio_artifact_id: UUID | None = None
    audio_version_id: UUID | None = None
    message: str | None = None


class MusicStudioService:
    """Launch and materialize the bounded, credential-free Music Studio workflow."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        task_service: TaskApplicationService,
        registry_service: AgentRegistryService,
        business_object_service: BusinessObjectService,
        artifact_service: ArtifactService,
        tenant_id: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._tasks = task_service
        self._registry = registry_service
        self._objects = business_object_service
        self._artifacts = artifact_service
        self._tenant_id = tenant_id
        self._provider = DeterministicMusicProvider()
        self._analyzer = DeterministicAudioAnalyzer()

    def launch(
        self,
        *,
        title: str,
        audience: str,
        language: str,
        mood: str,
        themes: list[str],
        genre_attributes: list[str],
        max_rounds: int,
        requested_by: str,
        idempotency_key: str,
    ) -> MusicProjectLaunch:
        brief = self._brief(title, audience, language, mood, themes, genre_attributes, max_rounds)
        company_id = self._installed_company_id()
        self._ensure_demo_workforce(company_id, requested_by)
        plan = CoordinatedPlan.create(
            (
                self._spec("brief", "Normalize the creative brief", "brief"),
                self._spec("trend", "Derive original trend attributes", "trend", ("brief",)),
                self._spec("lyrics", "Write original lyrics", "lyrics", ("brief", "trend")),
                self._spec(
                    "production",
                    "Create the provider-neutral composition specification",
                    "production",
                    ("brief", "trend"),
                ),
                self._spec(
                    "generation",
                    "Generate a bounded audio candidate",
                    "generation",
                    ("lyrics", "production"),
                ),
                self._spec(
                    "listening",
                    "Review the actual generated audio evidence",
                    "listening",
                    ("generation",),
                ),
            ),
            max_concurrency=2,
        )
        task = self._tasks.create_task(
            f"Produce the music project: {brief['title']}",
            {
                "workflow": WORKFLOW_KEY,
                "company_id": str(company_id),
                "brief": brief,
                "provider": self._provider.provider_name,
                "external_writes_enabled": False,
            },
            execution_mode=TaskExecutionMode.COORDINATED,
            coordinated_plan=plan,
            goal_constraints=(
                "Use original content",
                "Do not imitate an identifiable artist or voice",
                f"Stop after at most {max_rounds} rounds",
            ),
            goal_success_criteria=(
                "Produce one playable audio candidate",
                "Cite actual audio-derived review evidence",
                "Wait for owner approval before final release",
            ),
            idempotency_key=f"music-project:{idempotency_key.strip()}",
        )
        project_type = self._type(company_id, "music-project")
        project = self._get_or_create(
            company_id,
            project_type.id,
            f"music-task:{task.task.id}:project",
            {**brief, "use_plan": "internal-demo", "task_id": str(task.task.id)},
            actor=requested_by,
            source_type=ObjectSourceType.USER,
            source_id=str(task.task.id),
            owner_position_id=self._position(company_id, "creative-director").id,
            evidence_refs=[f"task:{task.task.id}:goal-contract"],
        )
        if task.task.status is TaskStatus.CREATED:
            task = self._tasks.request_run(task.task.id)
        return MusicProjectLaunch(task=task, project=project)

    def status(self, task_id: UUID) -> MusicProjectResult:
        task = self._task(task_id)
        company_id = UUID(str(task.task.input["company_id"]))
        project = self._external(company_id, "music-project", f"music-task:{task_id}:project")
        release = self._external(
            company_id, "final-release-package", f"music-task:{task_id}:release"
        )
        if release is None:
            state = "READY" if task.task.status is TaskStatus.COMPLETED else "WORKING"
            return MusicProjectResult(
                task_id=task_id,
                status=state,
                project_id=project.object.id,
                message="Result has not been materialized yet.",
            )
        data = release.revisions[-1].data
        return MusicProjectResult(
            task_id=task_id,
            status=(
                "APPROVED" if release.object.lifecycle_state == "APPROVED" else "WAITING_APPROVAL"
            ),
            project_id=project.object.id,
            candidate_id=UUID(data["candidate_id"]),
            review_id=UUID(data["review_id"]),
            release_id=release.object.id,
            audio_artifact_id=UUID(data["audio_artifact_id"]),
            audio_version_id=UUID(data["audio_version_id"]),
        )

    def materialize(self, task_id: UUID, *, actor: str) -> MusicProjectResult:
        current = self.status(task_id)
        if current.release_id is not None:
            return current
        task = self._task(task_id)
        if task.task.status is not TaskStatus.COMPLETED:
            raise InvalidCompanyPack("Music Project Task must complete before materialization")
        company_id = UUID(str(task.task.input["company_id"]))
        brief = dict(task.task.input["brief"])
        subtasks = {value.key: value for value in task.subtasks}
        generation_run = subtasks["generation"].current_run_id
        listening_run = subtasks["listening"].current_run_id
        generated = self._provider.generate(
            operation_key=f"music-task:{task_id}:round:1:candidate:a",
            seed=json.dumps(brief, sort_keys=True, ensure_ascii=False),
        )
        analysis = self._analyzer.analyze(generated.content)
        lyrics_text = self._lyrics(brief)
        lyrics = self._artifacts.create_artifact(
            display_name=f"{brief['title']} lyrics.txt",
            kind="music.lyrics",
            classification=ArtifactClassification.INTERNAL,
            media_type="text/plain",
            content=lyrics_text.encode(),
            idempotency_key=f"music-task:{task_id}:lyrics",
        )
        audio = self._artifacts.create_artifact(
            display_name=f"{brief['title']} candidate.wav",
            kind="music.audio-candidate",
            classification=ArtifactClassification.INTERNAL,
            media_type="audio/wav",
            content=generated.content,
            expected_sha256=generated.content_sha256,
            producer_run_id=generation_run,
            idempotency_key=f"music-task:{task_id}:audio:1:a",
        )
        evidence = self._artifacts.create_artifact(
            display_name=f"{brief['title']} audio evidence.json",
            kind="music.audio-evidence",
            classification=ArtifactClassification.INTERNAL,
            media_type="application/json",
            content=json.dumps(analysis.to_dict(), sort_keys=True).encode(),
            producer_run_id=listening_run,
            idempotency_key=f"music-task:{task_id}:audio-evidence:1:a",
        )
        rights = self._artifacts.create_artifact(
            display_name=f"{brief['title']} rights manifest.json",
            kind="music.rights-manifest",
            classification=ArtifactClassification.INTERNAL,
            media_type="application/json",
            content=json.dumps(
                {
                    "provider": generated.provider,
                    "use_plan": "internal-demo",
                    "external_distribution": False,
                    "voice_cloning": False,
                    "owner_approval_required": True,
                },
                sort_keys=True,
            ).encode(),
            idempotency_key=f"music-task:{task_id}:rights",
        )
        project = self._external(company_id, "music-project", f"music-task:{task_id}:project")
        assert project is not None
        self._create_supporting_objects(
            company_id, task_id, project.object.id, brief, lyrics, actor
        )
        candidate = self._get_or_create(
            company_id,
            self._type(company_id, "audio-candidate").id,
            f"music-task:{task_id}:candidate:1:a",
            {
                "project_id": str(project.object.id),
                "round": 1,
                "variant": "A",
                "audio_artifact_id": str(audio.artifact.id),
                "audio_version_id": str(audio.versions[0].id),
                "audio_digest": generated.content_sha256,
                "provider": generated.provider,
            },
            actor=actor,
            source_id=str(task_id),
            owner_position_id=self._position(company_id, "generation-operator").id,
            evidence_refs=[f"artifact:{audio.artifact.id}:version:{audio.versions[0].id}"],
        )
        review = self._get_or_create(
            company_id,
            self._type(company_id, "listening-review").id,
            f"music-task:{task_id}:review:1:a",
            {
                "project_id": str(project.object.id),
                "candidate_id": str(candidate.object.id),
                "overall_score": 84,
                "evidence_artifact_id": str(evidence.artifact.id),
                "findings": [
                    "The WAV container and duration are valid.",
                    "No clipped samples were detected.",
                    "Owner judgment is required for creative acceptance.",
                ],
                "decision": "SHORTLIST",
            },
            actor=actor,
            source_id=str(task_id),
            owner_position_id=self._position(company_id, "audio-critic").id,
            evidence_refs=[f"artifact:{evidence.artifact.id}:version:{evidence.versions[0].id}"],
        )
        release = self._get_or_create(
            company_id,
            self._type(company_id, "final-release-package").id,
            f"music-task:{task_id}:release",
            {
                "project_id": str(project.object.id),
                "candidate_id": str(candidate.object.id),
                "audio_artifact_id": str(audio.artifact.id),
                "audio_version_id": str(audio.versions[0].id),
                "lyrics_artifact_id": str(lyrics.artifact.id),
                "review_id": str(review.object.id),
                "rights_manifest_artifact_id": str(rights.artifact.id),
            },
            actor=actor,
            source_id=str(task_id),
            owner_position_id=self._position(company_id, "owner").id,
            evidence_refs=[f"business-object:{review.object.id}", f"artifact:{audio.artifact.id}"],
        )
        if release.object.lifecycle_state == "DRAFT":
            release = self._objects.apply_action(
                company_id,
                release.object.id,
                action_key="submit",
                expected_revision=release.object.current_revision,
                input={},
                actor=actor,
                source_type=ObjectSourceType.AGENT,
                source_id=str(task_id),
                evidence_refs=[f"business-object:{review.object.id}"],
            )
        return MusicProjectResult(
            task_id=task_id,
            status="WAITING_APPROVAL",
            project_id=project.object.id,
            candidate_id=candidate.object.id,
            review_id=review.object.id,
            release_id=release.object.id,
            audio_artifact_id=audio.artifact.id,
            audio_version_id=audio.versions[0].id,
        )

    def approve(self, task_id: UUID, *, actor: str) -> MusicProjectResult:
        current = self.status(task_id)
        if current.release_id is None:
            raise InvalidCompanyPack("Materialize the Music Project before approval")
        if current.status == "APPROVED":
            return current
        company_id = UUID(str(self._task(task_id).task.input["company_id"]))
        release = self._objects.get_object(company_id, current.release_id)
        self._objects.apply_action(
            company_id,
            release.object.id,
            action_key="approve",
            expected_revision=release.object.current_revision,
            input={},
            actor=actor,
            source_type=ObjectSourceType.USER,
            source_id=str(task_id),
            evidence_refs=[f"owner-approval:{actor}"],
            actor_position_key="owner",
        )
        return self.status(task_id)

    def _create_supporting_objects(self, company_id, task_id, project_id, brief, lyrics, actor):
        common = dict(actor=actor, source_id=str(task_id))
        self._get_or_create(
            company_id,
            self._type(company_id, "trend-dossier").id,
            f"music-task:{task_id}:trend",
            {
                "project_id": str(project_id),
                "observed_at": self._task(task_id).task.created_at.isoformat(),
                "attributes": brief["genre_attributes"],
                "limitations": "Deterministic Demo evidence; no live market source.",
            },
            owner_position_id=self._position(company_id, "trend-researcher").id,
            evidence_refs=["fixture://music-trends-v1"],
            **common,
        )
        self._get_or_create(
            company_id,
            self._type(company_id, "lyrics-draft").id,
            f"music-task:{task_id}:lyrics:1",
            {
                "project_id": str(project_id),
                "version": 1,
                "language": brief["language"],
                "lyrics_artifact_id": str(lyrics.artifact.id),
                "revision_reason": "Initial draft",
            },
            owner_position_id=self._position(company_id, "lyricist").id,
            evidence_refs=[f"artifact:{lyrics.artifact.id}"],
            **common,
        )
        self._get_or_create(
            company_id,
            self._type(company_id, "composition-spec").id,
            f"music-task:{task_id}:composition:1",
            {
                "project_id": str(project_id),
                "version": 1,
                "tempo_range": "108-116 BPM",
                "arrangement": ["synth", "bass", "drums"],
                "song_form": ["verse", "chorus", "verse", "chorus"],
            },
            owner_position_id=self._position(company_id, "music-producer").id,
            evidence_refs=[f"task:{task_id}:subtask:production"],
            **common,
        )

    def _ensure_demo_workforce(self, company_id: UUID, actor: str) -> None:
        for key, (agent_name, position_key, title) in AGENTS.items():
            aggregate = self._registry.ensure_builtin_agent(
                agent_name,
                role=title,
                instructions=f"Perform the bounded Music Studio {title} responsibility.",
                description=f"Deterministic Music Studio {title} employee.",
                extra_tags=("music-studio", key),
            )
            version = aggregate.versions[0]
            with self._uow_factory() as uow:
                position = uow.company_model.get_position_by_key(company_id, position_key)
                if position is None:
                    raise InvalidCompanyPack(f"Music Position '{position_key}' is unavailable")
                if uow.company_model.get_active_appointment(position.id) is not None:
                    continue
                appointment = Appointment.create(
                    company_id=company_id,
                    position_id=position.id,
                    agent_definition_id=aggregate.definition.id,
                    agent_version_id=version.id,
                    appointed_by=actor,
                    reason="Install the deterministic Music Studio starter team.",
                )
                uow.company_model.add_appointment(appointment)
                uow.commit()

    def _task(self, task_id: UUID) -> TaskAggregate:
        task = self._tasks.get_task(task_id)
        if task.task.input.get("workflow") != WORKFLOW_KEY:
            raise InvalidCompanyPack("Task is not a Music Studio workflow")
        return task

    def _installed_company_id(self) -> UUID:
        with self._uow_factory() as uow:
            company = uow.company_model.get_active_company(self._tenant_id)
            if company is None:
                raise InvalidCompanyPack("Install Music Studio before creating a project")
            if uow.company_packs.get_installation(company.id, PACK_KEY) is None:
                raise InvalidCompanyPack("The active Company is not a Music Studio")
            return company.id

    def _type(self, company_id: UUID, key: str):
        with self._uow_factory() as uow:
            value = uow.business_objects.get_type_by_key(company_id, key, published_only=True)
            if value is None:
                raise InvalidCompanyPack(f"Music object type '{key}' is unavailable")
            return value

    def _position(self, company_id: UUID, key: str):
        with self._uow_factory() as uow:
            value = uow.company_model.get_position_by_key(company_id, key)
            if value is None:
                raise InvalidCompanyPack(f"Music Position '{key}' is unavailable")
            return value

    def _external(self, company_id: UUID, type_key: str, external_ref: str):
        object_type = self._type(company_id, type_key)
        with self._uow_factory() as uow:
            value = uow.business_objects.get_object_by_external_ref(object_type.id, external_ref)
        return self._objects.get_object(company_id, value.id) if value is not None else None

    def _get_or_create(
        self,
        company_id,
        type_id,
        external_ref,
        data,
        *,
        actor,
        source_id,
        owner_position_id,
        evidence_refs,
        source_type=ObjectSourceType.AGENT,
    ):
        with self._uow_factory() as uow:
            existing = uow.business_objects.get_object_by_external_ref(type_id, external_ref)
        if existing is not None:
            return self._objects.get_object(company_id, existing.id)
        return self._objects.create_object(
            company_id,
            type_id=type_id,
            data=data,
            actor=actor,
            source_type=source_type,
            source_id=source_id,
            external_ref=external_ref,
            owner_position_id=owner_position_id,
            evidence_refs=evidence_refs,
        )

    @staticmethod
    def _spec(key, objective, agent_key, depends_on=()):
        return SubtaskSpec.create(
            key=key,
            objective=objective,
            input={"position_key": AGENTS[agent_key][1]},
            required_capabilities=("general.task",),
            depends_on=depends_on,
            preferred_agent_id=AGENTS[agent_key][0],
        )

    @staticmethod
    def _brief(title, audience, language, mood, themes, genres, max_rounds):
        values = {
            "title": title.strip(),
            "audience": audience.strip(),
            "language": language.strip(),
            "mood": mood.strip(),
            "themes": [value.strip() for value in themes if value.strip()],
            "genre_attributes": [value.strip() for value in genres if value.strip()],
            "max_rounds": max_rounds,
        }
        if any(not values[key] for key in ("title", "audience", "language", "mood")):
            raise InvalidCompanyPack("Music Project brief fields are required")
        if not values["themes"] or not values["genre_attributes"] or not 1 <= max_rounds <= 5:
            raise InvalidCompanyPack(
                "Music Project themes, genre attributes, or rounds are invalid"
            )
        return values

    @staticmethod
    def _lyrics(brief: dict[str, Any]) -> str:
        themes = " / ".join(brief["themes"])
        return (
            f"# {brief['title']}\n\n[Verse]\nCity lights keep time with us tonight\n"
            f"We carry {themes} into the light\n\n[Chorus]\n"
            "One clear signal, one open sky\nWe make the moment ours tonight\n"
        )
