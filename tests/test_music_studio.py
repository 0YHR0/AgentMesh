import io
import json
import zipfile
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.domain.errors import CompanyPackConflict, InvalidCompanyPack
from agentmesh.domain.messaging import RUN_REQUESTED_SCHEMA
from agentmesh.features import FeatureGateSet
from agentmesh.templates.music_studio import build_pack, manifest


def test_music_studio_pack_is_stable_minimal_and_safe():
    first = build_pack()
    second = build_pack()
    resources = manifest()["resources"]

    assert first.content_digest == second.content_digest
    assert first.key == "agentmesh.music-studio"
    assert first.required_features == ["business_objects", "company_model"]
    assert sum(item["kind"] == "organization_unit" for item in resources) == 5
    assert sum(item["kind"] == "position" for item in resources) == 7
    assert sum(item["kind"] == "business_object_type" for item in resources) == 8
    assert manifest()["template"]["safety"] == {
        "external_writes_enabled": False,
        "artist_imitation_enabled": False,
        "voice_cloning_enabled": False,
        "distribution_enabled": False,
    }


def test_music_studio_template_provisions_company_atomically(
    company_pack_service,
    uow_factory,
):
    preview = company_pack_service.preview_music_studio_template()

    assert preview.installable
    assert preview.required_credentials == []
    assert preview.permissions == ["company:manage"]
    assert not preview.external_writes_enabled
    assert preview.resource_summary == {
        "organization_unit": 5,
        "position": 7,
        "business_object_type": 8,
    }

    result = company_pack_service.install_music_studio_template(
        company_name="North Star Music",
        owner_principal_id="owner",
        default_language="zh-CN",
        default_genre="dance-pop",
        use_plan="internal-demo",
        operating_timezone="Asia/Shanghai",
    )

    assert result.company.name == "North Star Music"
    assert result.installation.pack_digest == preview.content_digest
    assert result.installation.configuration == {
        "default_language": "zh-CN",
        "default_genre": "dance-pop",
        "use_plan": "internal-demo",
        "generation_provider": "deterministic-demo",
        "external_writes_enabled": False,
    }
    assert len(result.installation.resource_refs) == 20
    with uow_factory() as uow:
        assert len(uow.company_model.list_units(result.company.id)) == 5
        assert len(uow.company_model.list_positions(result.company.id)) == 7
        assert len(uow.company_packs.list_installations(result.company.id)) == 1
        assert (
            uow.business_objects.get_type_by_key(
                result.company.id,
                "final-release-package",
                published_only=True,
            )
            is not None
        )

    with pytest.raises(CompanyPackConflict, match="active Company"):
        company_pack_service.install_music_studio_template(
            company_name="Duplicate Music Studio",
            owner_principal_id="owner",
            default_language="en",
            default_genre="pop",
            use_plan="personal",
        )


def test_music_studio_template_rejects_invalid_configuration(company_pack_service):
    with pytest.raises(InvalidCompanyPack, match="Use plan"):
        company_pack_service.install_music_studio_template(
            company_name="Invalid Music Studio",
            owner_principal_id="owner",
            default_language="en",
            default_genre="pop",
            use_plan="publish-everywhere",
        )


def test_music_studio_template_api_preview_list_and_install(application_container):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,business_objects=true,company_packs=true",
    )
    with TestClient(create_app(application_container)) as client:
        templates = client.get("/api/v1/company-templates")
        assert templates.status_code == 200
        assert [value["slug"] for value in templates.json()] == [
            "music-studio",
            "market-intelligence-studio",
        ]

        preview = client.get("/api/v1/company-templates/music-studio/preview")
        assert preview.status_code == 200
        assert preview.json()["installable"]
        assert preview.json()["required_credentials"] == []

        installed = client.post(
            "/api/v1/company-templates/music-studio/install",
            json={
                "company_name": "API Music Studio",
                "default_language": "zh-CN",
                "default_genre": "city pop",
                "use_plan": "internal-demo",
                "operating_timezone": "Asia/Shanghai",
            },
        )
        assert installed.status_code == 201
        payload = installed.json()
        assert payload["company"]["name"] == "API Music Studio"
        assert payload["installation"]["configuration"]["default_genre"] == "city pop"
        assert len(payload["installation"]["resource_refs"]) == 20


def test_music_studio_demo_runs_to_owner_approved_release(
    application_container,
    execution_service,
    uow_factory,
):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        "company_model=true,business_objects=true,company_packs=true",
    )
    with TestClient(create_app(application_container)) as client:
        installed = client.post(
            "/api/v1/company-templates/music-studio/install",
            json={
                "company_name": "End-to-end Music Studio",
                "default_language": "en",
                "default_genre": "pop",
                "use_plan": "internal-demo",
            },
        )
        assert installed.status_code == 201

        launched = client.post(
            "/api/v1/music-studio/projects",
            headers={"Idempotency-Key": "first-song"},
            json={
                "title": "City Signal",
                "audience": "young adults",
                "language": "en",
                "mood": "warm and energetic",
                "themes": ["summer", "reunion"],
                "genre_attributes": ["dance-pop", "bright synths"],
                "max_rounds": 3,
            },
        )
        assert launched.status_code == 201, launched.text
        task_id = launched.json()["task"]["id"]
        assert launched.json()["task"]["status"] == "RUNNING"
        assert len(launched.json()["task"]["subtasks"]) == 6

        processed: set[str] = set()
        for _ in range(20):
            aggregate = application_container.task_service.get_task(UUID(task_id))
            if aggregate.task.status.value == "COMPLETED":
                break
            wakeups = [
                value
                for value in uow_factory.store.outbox
                if value.schema_name == RUN_REQUESTED_SCHEMA
                and value.payload["run_id"] not in processed
            ]
            assert wakeups
            for wakeup in wakeups:
                processed.add(wakeup.payload["run_id"])
                assert execution_service.process(wakeup) is True
        else:
            pytest.fail("Music Studio coordinated Task did not complete")

        materialized = client.post(f"/api/v1/music-studio/projects/{task_id}/materialize")
        assert materialized.status_code == 200, materialized.text
        result = materialized.json()
        assert result["status"] == "WAITING_APPROVAL"
        assert result["title"] == "City Signal"
        assert result["current_round"] == 1
        assert result["max_rounds"] == 3
        assert result["overall_score"] == 84
        assert result["findings"]
        assert result["audio_artifact_id"]
        assert result["audio_version_id"]
        assert [value["variant"] for value in result["candidates"]] == ["A", "B"]
        assert [value["selected"] for value in result["candidates"]] == [True, False]

        audio = client.get(f"/api/v1/artifact-versions/{result['audio_version_id']}/content")
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/wav"
        assert audio.content[:4] == b"RIFF"

        selected = client.post(
            f"/api/v1/music-studio/projects/{task_id}/select",
            json={"candidate_id": result["candidates"][1]["candidate_id"]},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["overall_score"] == 86
        assert [value["selected"] for value in selected.json()["candidates"]] == [
            False,
            True,
        ]

        revised = client.post(
            f"/api/v1/music-studio/projects/{task_id}/revision",
            headers={"Idempotency-Key": "warmer-chorus"},
            json={
                "failed_criterion": "The chorus feels too restrained",
                "requested_change": "Make the chorus brighter and more energetic",
            },
        )
        assert revised.status_code == 200, revised.text
        round_two = revised.json()
        assert round_two["status"] == "WAITING_APPROVAL"
        assert round_two["current_round"] == 2
        assert round_two["audio_version_id"] != result["audio_version_id"]
        assert "The chorus feels too restrained" in round_two["findings"][0]

        replay = client.post(
            f"/api/v1/music-studio/projects/{task_id}/revision",
            headers={"Idempotency-Key": "warmer-chorus"},
            json={
                "failed_criterion": "The chorus feels too restrained",
                "requested_change": "Make the chorus brighter and more energetic",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["current_round"] == 2
        assert replay.json()["audio_version_id"] == round_two["audio_version_id"]

        conflicting_replay = client.post(
            f"/api/v1/music-studio/projects/{task_id}/revision",
            headers={"Idempotency-Key": "warmer-chorus"},
            json={
                "failed_criterion": "A different criterion",
                "requested_change": "A different requested change",
            },
        )
        assert conflicting_replay.status_code == 422
        assert "different input" in conflicting_replay.json()["message"]

        final_revision = client.post(
            f"/api/v1/music-studio/projects/{task_id}/revision",
            headers={"Idempotency-Key": "clearer-ending"},
            json={
                "failed_criterion": "The ending is not decisive",
                "requested_change": "Give the ending a shorter final cadence",
            },
        )
        assert final_revision.status_code == 200
        assert final_revision.json()["current_round"] == 3
        assert len(final_revision.json()["candidates"]) == 2

        exhausted = client.post(
            f"/api/v1/music-studio/projects/{task_id}/revision",
            headers={"Idempotency-Key": "one-too-many"},
            json={
                "failed_criterion": "Owner wants another option",
                "requested_change": "Generate one more candidate",
            },
        )
        assert exhausted.status_code == 422
        assert exhausted.json()["code"] == "invalid_company_pack"
        assert "revision limit" in exhausted.json()["message"]

        final_b = final_revision.json()["candidates"][1]
        final_selection = client.post(
            f"/api/v1/music-studio/projects/{task_id}/select",
            json={"candidate_id": final_b["candidate_id"]},
        )
        assert final_selection.status_code == 200
        assert final_selection.json()["audio_version_id"] == final_b["audio_version_id"]

        approved = client.post(f"/api/v1/music-studio/projects/{task_id}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "APPROVED"
        assert approved.json()["package_artifact_id"]
        package_version_id = approved.json()["package_version_id"]
        package = client.get(f"/api/v1/artifact-versions/{package_version_id}/content")
        assert package.status_code == 200
        assert package.headers["content-type"] == "application/zip"
        assert "filename=" in package.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
            assert archive.namelist() == [
                "audio.wav",
                "lyrics.txt",
                "rights-manifest.json",
                "release.json",
            ]
            assert archive.read("audio.wav")[:4] == b"RIFF"
            release_manifest = json.loads(archive.read("release.json"))
            assert release_manifest["candidate_id"] == final_b["candidate_id"]
