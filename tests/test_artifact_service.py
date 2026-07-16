from uuid import uuid4

import pytest

from agentmesh.application.artifact_services import ArtifactService
from agentmesh.domain.artifacts import ArtifactClassification
from agentmesh.domain.errors import ArtifactNotFound, IdempotencyConflict, InvalidArtifact
from tests.fakes import InMemoryUnitOfWorkFactory


def test_create_and_version_artifact(
    artifact_service: ArtifactService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = artifact_service.create_artifact(
        display_name="result.json",
        kind="task.result",
        classification=ArtifactClassification.INTERNAL,
        media_type="application/json",
        content=b'{"status":"created"}',
        idempotency_key="artifact-1",
    )
    replay = artifact_service.create_artifact(
        display_name=" result.json ",
        kind="TASK.RESULT",
        classification=ArtifactClassification.INTERNAL,
        media_type="APPLICATION/JSON",
        content=b'{"status":"created"}',
        idempotency_key="artifact-1",
    )
    updated = artifact_service.add_version(
        created.artifact.id,
        media_type="application/json",
        content=b'{"status":"updated"}',
        idempotency_key="version-2",
    )

    assert replay.artifact.id == created.artifact.id
    assert created.versions[0].version_number == 1
    assert [value.version_number for value in updated.versions] == [1, 2]
    assert updated.artifact.version_count == 2
    assert len(uow_factory.store.outbox) == 3
    assert uow_factory.store.outbox[-1].schema_name == "agentmesh.artifact-version.available"
    assert "content" not in uow_factory.store.outbox[-1].payload


def test_artifact_idempotency_key_rejects_different_content(
    artifact_service: ArtifactService,
) -> None:
    values = {
        "display_name": "result.txt",
        "kind": "task.result",
        "classification": ArtifactClassification.INTERNAL,
        "media_type": "text/plain",
        "idempotency_key": "shared-key",
    }
    artifact_service.create_artifact(content=b"first", **values)

    with pytest.raises(IdempotencyConflict):
        artifact_service.create_artifact(content=b"second", **values)


def test_artifact_access_is_tenant_scoped(
    artifact_service: ArtifactService,
    uow_factory: InMemoryUnitOfWorkFactory,
) -> None:
    created = artifact_service.create_artifact(
        display_name="private.txt",
        kind="document.text",
        classification=ArtifactClassification.CONFIDENTIAL,
        media_type="text/plain",
        content=b"tenant-private",
    )
    another_tenant = ArtifactService(
        uow_factory=uow_factory,
        tenant_id="another-tenant",
        owner_id="another-user",
        max_inline_bytes=65_536,
    )

    with pytest.raises(ArtifactNotFound):
        another_tenant.get_artifact(created.artifact.id)
    with pytest.raises(ArtifactNotFound):
        another_tenant.get_version_content(created.versions[0].id)


def test_producer_run_must_belong_to_current_tenant(
    artifact_service: ArtifactService,
) -> None:
    with pytest.raises(InvalidArtifact, match="producer_run_id"):
        artifact_service.create_artifact(
            display_name="result.txt",
            kind="task.result",
            classification=ArtifactClassification.INTERNAL,
            media_type="text/plain",
            content=b"result",
            producer_run_id=uuid4(),
        )
