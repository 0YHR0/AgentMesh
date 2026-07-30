from __future__ import annotations

from copy import deepcopy
from typing import Any

from agentmesh.domain.company_packs import CompanyPack, PackKind
from agentmesh.templates.market_intelligence_studio import PACK_KEY as BASE_PACK_KEY

PACK_KEY = "agentmesh.market-intelligence-operations"
PACK_VERSION = "1.0.0"
PACK_NAME = "Market Intelligence Operations"
DEFAULT_CYCLE_DAYS = 28
DEFAULT_BUDGET_LIMIT_MICROS = 10_000_000


def manifest() -> dict[str, Any]:
    resources: list[dict[str, Any]] = [
        {
            "kind": "budget_allocation",
            "key": "initial-company-budget",
            "scope_type": "COMPANY",
            "scope_id": "company",
            "policy_version": 1,
            "default_limit_micros": DEFAULT_BUDGET_LIMIT_MICROS,
        },
        {
            "kind": "operating_cycle",
            "key": "validation-cycle",
            "name": "First Market Validation Cycle",
            "duration_days": DEFAULT_CYCLE_DAYS,
            "review_schedule": {"cadence": "weekly", "final_review": True},
            "activate": True,
        },
        {
            "kind": "objective",
            "key": "validate-intelligence-product",
            "cycle_key": "validation-cycle",
            "owner_position_key": "owner",
            "statement": (
                "Validate and deliver one trustworthy market-intelligence product "
                "for the approved niche."
            ),
            "rationale": (
                "The first cycle must prove evidence quality, delivery discipline, "
                "budget control, and measurable customer interest."
            ),
            "priority": 1,
            "activate": True,
        },
        {
            "kind": "key_result",
            "key": "approved-report-count",
            "objective_key": "validate-intelligence-product",
            "metric_key": "approved_report_count",
            "unit": "count",
            "baseline": "0",
            "target": "1",
            "measurement_source": "research-report lifecycle",
        },
        {
            "kind": "key_result",
            "key": "verified-claim-ratio",
            "objective_key": "validate-intelligence-product",
            "metric_key": "verified_claim_ratio",
            "unit": "percent",
            "baseline": "0",
            "target": "100",
            "measurement_source": "claim-register lifecycle",
        },
        {
            "kind": "key_result",
            "key": "budget-compliance",
            "objective_key": "validate-intelligence-product",
            "metric_key": "budget_compliance",
            "unit": "boolean",
            "baseline": "0",
            "target": "1",
            "measurement_source": "financial governance ledger",
        },
        {
            "kind": "key_result",
            "key": "qualified-interest-count",
            "objective_key": "validate-intelligence-product",
            "metric_key": "qualified_interest_count",
            "unit": "count",
            "baseline": "0",
            "target": "3",
            "measurement_source": "commercial-opportunity lifecycle",
        },
        {
            "kind": "initiative",
            "key": "first-intelligence-release",
            "objective_key": "validate-intelligence-product",
            "owner_unit_key": "product",
            "budget_allocation_key": "initial-company-budget",
            "title": "Produce and validate the first intelligence release",
            "outcome_contract": {
                "deliverable": "one approved research report",
                "evidence": [
                    "approved research question",
                    "attributable source records",
                    "approved claim register",
                    "fact and editorial review",
                ],
                "external_publish_requires_approval": True,
            },
            "activate": True,
        },
        {
            "kind": "memory_policy",
            "key": "company-knowledge",
            "version": 1,
            "readable_namespace_patterns": [
                "company/*",
                "unit/*",
                "project/*",
            ],
            "writable_namespace_patterns": [
                "company/*",
                "unit/*",
                "project/*",
            ],
            "allowed_memory_types": [
                "FACT",
                "DECISION",
                "PROCEDURE",
                "PATTERN",
                "FEEDBACK",
            ],
            "auto_accept_memory_types": [],
            "forbidden_sensitivity_levels": ["RESTRICTED"],
            "maximum_retrieval_count": 10,
            "maximum_context_tokens": 4000,
            "review_role": "company-owner",
            "extraction_enabled": False,
        },
        {
            "kind": "company_operation",
            "key": "weekly-research-planning",
            "unit_key": "research",
            "initiative_key": "first-intelligence-release",
            "name": "Weekly Research Planning",
            "objective_template": (
                "Review the active research question, evidence gaps, and next "
                "source-collection actions. Produce an internal plan only."
            ),
            "input_template": {"workflow": "research-planning"},
            "trigger_kind": "INTERVAL",
            "trigger_definition": {"interval_seconds": 604800},
            "missed_policy": "LATEST",
            "position_keys": ["research-lead", "research-specialist"],
        },
        {
            "kind": "company_operation",
            "key": "weekly-evidence-review",
            "unit_key": "review-risk",
            "initiative_key": "first-intelligence-release",
            "name": "Weekly Evidence Review",
            "objective_template": (
                "Audit material claims against attributable sources and report "
                "unsupported claims. Do not publish externally."
            ),
            "input_template": {"workflow": "evidence-review"},
            "trigger_kind": "INTERVAL",
            "trigger_definition": {"interval_seconds": 604800},
            "missed_policy": "REQUIRE_REVIEW",
            "position_keys": ["fact-reviewer", "editorial-reviewer"],
        },
        {
            "kind": "company_operation",
            "key": "weekly-commercial-review",
            "unit_key": "growth-customer",
            "initiative_key": "first-intelligence-release",
            "name": "Weekly Commercial Review",
            "objective_template": (
                "Review verified customer-interest evidence and propose the next "
                "bounded experiment. External outreach remains approval-gated."
            ),
            "input_template": {"workflow": "commercial-review"},
            "trigger_kind": "INTERVAL",
            "trigger_definition": {"interval_seconds": 604800},
            "missed_policy": "SKIP",
            "position_keys": ["growth-strategist", "sales-researcher"],
        },
    ]
    return {
        "operations": {
            "base_template": BASE_PACK_KEY,
            "configuration_fields": [
                "starts_at",
                "cycle_days",
                "budget_limit_micros",
                "currency",
            ],
            "safety": {
                "operations_start_in_draft": True,
                "external_writes_enabled": False,
            },
        },
        "resources": deepcopy(resources),
    }


def build_pack() -> CompanyPack:
    return CompanyPack.create(
        key=PACK_KEY,
        version=PACK_VERSION,
        name=PACK_NAME,
        kind=PackKind.DOMAIN,
        manifest=manifest(),
        dependencies=[BASE_PACK_KEY],
        required_features=[
            "company_goals",
            "company_operations",
            "organizational_memory",
            "financial_governance",
        ],
    )
