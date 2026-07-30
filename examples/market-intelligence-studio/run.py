"""Run the deterministic Market Intelligence Studio evidence chain."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = os.getenv("AGENTMESH_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("AGENTMESH_TOKEN", "").strip()
FIXTURES = Path(__file__).parent / "fixtures"


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    value = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(value, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} failed: {exc.code} {detail}") from exc


def create_object(
    company_id: str,
    type_id: str,
    data: dict[str, Any],
    evidence_refs: list[str],
) -> dict[str, Any]:
    return request(
        "POST",
        f"/api/v1/companies/{company_id}/business-objects",
        {
            "type_id": type_id,
            "data": data,
            "source_type": "IMPORT",
            "source_id": "offline-market-intelligence-fixture-v1",
            "evidence_refs": evidence_refs,
        },
    )


def transition(
    company_id: str,
    snapshot: dict[str, Any],
    action: str,
    position: str,
    evidence_ref: str,
) -> dict[str, Any]:
    object_id = snapshot["object"]["id"]
    return request(
        "POST",
        f"/api/v1/companies/{company_id}/business-objects/{object_id}/actions",
        {
            "action_key": action,
            "expected_revision": snapshot["object"]["current_revision"],
            "source_type": "SYSTEM",
            "source_id": "offline-market-intelligence-fixture-v1",
            "evidence_refs": [evidence_ref],
            "actor_position_key": position,
        },
    )


def approve(
    company_id: str,
    snapshot: dict[str, Any],
    position: str,
    evidence_prefix: str,
) -> dict[str, Any]:
    submitted = transition(
        company_id, snapshot, "submit", position, f"{evidence_prefix}:submission"
    )
    approved = transition(
        company_id, submitted, "approve", position, f"{evidence_prefix}:approval"
    )
    if approved["object"]["lifecycle_state"] != "APPROVED":
        raise RuntimeError(f"{approved['type']['key']} did not reach APPROVED")
    if not approved["revisions"][-1]["evidence_refs"]:
        raise RuntimeError(f"{approved['type']['key']} approval has no evidence")
    return approved


def main() -> None:
    preview = request(
        "GET", "/api/v1/company-templates/market-intelligence-studio/preview"
    )
    if preview["required_credentials"] or preview["external_writes_enabled"]:
        raise RuntimeError("Offline template safety boundary changed")
    installed = request(
        "POST",
        "/api/v1/company-templates/market-intelligence-studio/install",
        {
            "company_name": "Offline Market Intelligence Studio",
            "target_market": "Platform engineering teams evaluating research services",
            "product_type": "research-report",
            "excluded_sectors": ["weapons", "gambling"],
            "operating_timezone": "UTC",
        },
    )
    company_id = installed["company"]["id"]
    types = request(
        "GET", f"/api/v1/companies/{company_id}/business-object-types"
    )
    type_ids = {value["key"]: value["id"] for value in types}

    question = approve(
        company_id,
        create_object(
            company_id,
            type_ids["research-question"],
            {
                "question": "What evidence would support a traceable research product?",
                "target_audience": "Platform engineering leaders",
                "decision_supported": "Whether to commission a recurring report",
            },
            ["fixture://research-question-v1"],
        ),
        "research-lead",
        "fixture://research-question-v1",
    )

    excerpt = (FIXTURES / "source-excerpt.txt").read_bytes()
    source = approve(
        company_id,
        create_object(
            company_id,
            type_ids["source-record"],
            {
                "title": "Synthetic platform-team interview fixture",
                "uri": "fixture://source-excerpt.txt",
                "publisher": "AgentMesh deterministic showcase",
                "retrieved_at": "2026-07-30T00:00:00Z",
                "excerpt_digest": hashlib.sha256(excerpt).hexdigest(),
            },
            [f"business-object:{question['object']['id']}"],
        ),
        "research-lead",
        "fixture://source-excerpt.txt",
    )

    claim = approve(
        company_id,
        create_object(
            company_id,
            type_ids["claim-register"],
            {
                "claim": (
                    "The synthetic fixture indicates demand for reproducible, "
                    "source-linked intelligence."
                ),
                "source_record_ids": [source["object"]["id"]],
                "confidence": "MEDIUM",
                "limitations": "Synthetic fixture; not a real-world market statistic.",
            },
            [f"business-object:{source['object']['id']}"],
        ),
        "fact-reviewer",
        f"business-object:{source['object']['id']}",
    )

    report = approve(
        company_id,
        create_object(
            company_id,
            type_ids["research-report"],
            {
                "title": "Offline fixture report",
                "audience": "Platform engineering leaders",
                "claim_register_ids": [claim["object"]["id"]],
                "artifact_id": "fixture://expected-report.md",
            },
            [f"business-object:{claim['object']['id']}"],
        ),
        "editorial-reviewer",
        f"business-object:{claim['object']['id']}",
    )
    print(
        json.dumps(
            {
                "company_id": company_id,
                "pack_digest": installed["installation"]["pack_digest"],
                "approved_report_id": report["object"]["id"],
                "claim_register_id": claim["object"]["id"],
                "external_writes": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
