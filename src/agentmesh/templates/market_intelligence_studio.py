from __future__ import annotations

from copy import deepcopy
from typing import Any

from agentmesh.domain.company_packs import CompanyPack, PackKind

TEMPLATE_SLUG = "market-intelligence-studio"
PACK_KEY = "agentmesh.market-intelligence-studio"
PACK_VERSION = "1.0.0"
PACK_NAME = "AgentMesh Market Intelligence Studio"
DEFAULT_MISSION = (
    "Turn verified market evidence into useful, trustworthy business intelligence."
)
PRODUCT_TYPES = ("research-report", "subscription", "custom-research")


def _unit(key: str, name: str, purpose: str) -> dict[str, Any]:
    return {
        "kind": "organization_unit",
        "key": key,
        "name": name,
        "purpose": purpose,
        "memory_namespace": f"company/{key}",
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
    budget_scope: dict[str, Any] | None = None,
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
        "budget_scope": budget_scope or {},
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
) -> dict[str, Any]:
    return {
        "kind": "business_object_type",
        "key": key,
        "name": name,
        "json_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "lifecycle_definition": {
            "states": ["DRAFT", "IN_REVIEW", "APPROVED", "RETIRED"],
            "initial_state": "DRAFT",
            "actions": {
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
            },
        },
        "ownership_rules": {"review_position_key": review_position},
        "retention_policy": {"minimum_days": 365},
    }


def manifest() -> dict[str, Any]:
    resources: list[dict[str, Any]] = [
        _unit("executive", "Executive", "Set direction, priorities, and risk boundaries."),
        _unit("product", "Product", "Convert market needs into useful research products."),
        _unit("research", "Research", "Collect attributable primary and secondary evidence."),
        _unit("analysis", "Analysis", "Turn evidence into reproducible market findings."),
        _unit("content", "Content & Design", "Create clear, accessible client deliverables."),
        _unit("review-risk", "Review & Risk", "Verify claims, editorial quality, and risk."),
        _unit("growth-customer", "Growth & Customer", "Find demand and retain customers."),
        _unit("finance", "Finance", "Protect budget integrity and commercial evidence."),
        _position(
            "owner",
            "executive",
            "Owner",
            "A profitable, trustworthy research company operating within explicit risk limits.",
            ["company.strategy", "company.governance"],
            approval_scope={"external_publish": True, "commercial_commitment": True},
            budget_scope={"company": True},
        ),
        _position(
            "chief-of-staff",
            "executive",
            "Chief of Staff",
            "Cross-department priorities are explicit, scheduled, and followed through.",
            ["company.coordination", "task.planning"],
            reports_to="owner",
        ),
        _position(
            "coo",
            "executive",
            "COO",
            "Recurring company operations remain reliable and measurable.",
            ["company.operations", "risk.control"],
            reports_to="owner",
        ),
        _position(
            "product-strategist",
            "product",
            "Product Strategist",
            "Research products address a verified customer decision.",
            ["product.strategy", "customer.discovery"],
            reports_to="chief-of-staff",
        ),
        _position(
            "research-lead",
            "research",
            "Research Lead",
            "Research plans are answerable, attributable, and methodologically sound.",
            ["research.plan", "evidence.review"],
            reports_to="chief-of-staff",
            tools=["web.search", "source.read"],
        ),
        _position(
            "research-specialist",
            "research",
            "Research Specialist",
            "Source records are attributable, relevant, and reproducible.",
            ["research.collect", "source.verify"],
            reports_to="research-lead",
            tools=["web.search", "source.read"],
        ),
        _position(
            "market-analyst",
            "analysis",
            "Market Analyst",
            "Market claims follow from cited evidence and state uncertainty.",
            ["market.analysis", "claim.synthesis"],
            reports_to="research-lead",
        ),
        _position(
            "data-analyst",
            "analysis",
            "Data Analyst",
            "Quantitative findings are reproducible and quality checked.",
            ["data.analysis", "data.quality"],
            reports_to="research-lead",
            tools=["dataset.read"],
        ),
        _position(
            "writer",
            "content",
            "Writer",
            "Approved findings become a decision-ready narrative without unsupported claims.",
            ["content.write", "claim.trace"],
            reports_to="product-strategist",
        ),
        _position(
            "designer",
            "content",
            "Designer",
            "Reports communicate evidence and uncertainty clearly.",
            ["content.design", "data.visualization"],
            reports_to="product-strategist",
        ),
        _position(
            "fact-reviewer",
            "review-risk",
            "Fact Reviewer",
            "Every material published claim is supported by admissible evidence.",
            ["evidence.audit", "claim.review"],
            reports_to="coo",
            approval_scope={"claim_approval": True},
        ),
        _position(
            "editorial-reviewer",
            "review-risk",
            "Editorial Reviewer",
            "Deliverables meet the editorial contract and distinguish fact from inference.",
            ["editorial.review", "content.quality"],
            reports_to="coo",
            approval_scope={"report_approval": True},
        ),
        _position(
            "risk-officer",
            "review-risk",
            "Risk Officer",
            "Sensitive sectors, claims, and external actions remain inside policy.",
            ["risk.review", "policy.enforce"],
            reports_to="owner",
            approval_scope={"risk_exception": True},
        ),
        _position(
            "growth-strategist",
            "growth-customer",
            "Growth Strategist",
            "Growth experiments target explicit audiences and measurable demand.",
            ["growth.strategy", "experiment.design"],
            reports_to="chief-of-staff",
        ),
        _position(
            "sales-researcher",
            "growth-customer",
            "Sales Researcher",
            "Commercial opportunities are qualified with evidence before outreach.",
            ["sales.research", "opportunity.qualify"],
            reports_to="growth-strategist",
        ),
        _position(
            "customer-success",
            "growth-customer",
            "Customer Success",
            "Customer outcomes and feedback improve future research products.",
            ["customer.success", "feedback.synthesis"],
            reports_to="growth-strategist",
        ),
        _position(
            "finance-controller",
            "finance",
            "Finance Controller",
            "Costs, revenue evidence, and expense decisions remain auditable.",
            ["finance.control", "budget.review"],
            reports_to="owner",
            approval_scope={"expense_review": True},
            budget_scope={"finance": True},
        ),
        _object_type(
            "research-question",
            "Research Question",
            {
                "question": {"type": "string", "minLength": 10},
                "target_audience": {"type": "string"},
                "decision_supported": {"type": "string"},
            },
            ["question", "target_audience", "decision_supported"],
            review_position="research-lead",
        ),
        _object_type(
            "source-record",
            "Source Record",
            {
                "title": {"type": "string"},
                "uri": {"type": "string"},
                "publisher": {"type": "string"},
                "retrieved_at": {"type": "string", "format": "date-time"},
                "excerpt_digest": {"type": "string"},
            },
            ["title", "uri", "publisher", "retrieved_at", "excerpt_digest"],
            review_position="research-lead",
        ),
        _object_type(
            "claim-register",
            "Claim Register",
            {
                "claim": {"type": "string"},
                "source_record_ids": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                "limitations": {"type": "string"},
            },
            ["claim", "source_record_ids", "confidence", "limitations"],
            review_position="fact-reviewer",
        ),
        _object_type(
            "research-report",
            "Research Report",
            {
                "title": {"type": "string"},
                "audience": {"type": "string"},
                "claim_register_ids": {"type": "array", "items": {"type": "string"}},
                "artifact_id": {"type": "string"},
            },
            ["title", "audience", "claim_register_ids", "artifact_id"],
            review_position="editorial-reviewer",
        ),
        _object_type(
            "commercial-opportunity",
            "Commercial Opportunity",
            {
                "organization": {"type": "string"},
                "problem": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "stage": {"type": "string"},
            },
            ["organization", "problem", "evidence", "stage"],
            review_position="growth-strategist",
        ),
        _object_type(
            "offer",
            "Offer",
            {
                "name": {"type": "string"},
                "product_type": {"type": "string"},
                "price_micros": {"type": "integer", "minimum": 0},
                "currency": {"type": "string"},
                "scope": {"type": "string"},
            },
            ["name", "product_type", "price_micros", "currency", "scope"],
            review_position="finance-controller",
        ),
        _object_type(
            "customer-feedback",
            "Customer Feedback",
            {
                "customer_ref": {"type": "string"},
                "report_id": {"type": "string"},
                "outcome": {"type": "string"},
                "feedback": {"type": "string"},
            },
            ["customer_ref", "report_id", "outcome", "feedback"],
            review_position="customer-success",
        ),
    ]
    return {
        "template": {
            "slug": TEMPLATE_SLUG,
            "mission": DEFAULT_MISSION,
            "configuration_fields": [
                "target_market",
                "product_type",
                "excluded_sectors",
            ],
        },
        "resources": deepcopy(resources),
    }


def build_pack() -> CompanyPack:
    return CompanyPack.create(
        key=PACK_KEY,
        version=PACK_VERSION,
        name=PACK_NAME,
        kind=PackKind.TEMPLATE,
        manifest=manifest(),
        required_features=["business_objects", "company_model"],
    )

