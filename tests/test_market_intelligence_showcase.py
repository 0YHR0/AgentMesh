import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient

from agentmesh.api.app import create_app
from agentmesh.features import FeatureGateSet


def test_offline_market_intelligence_showcase_reaches_approved_report(
    application_container,
    capsys,
):
    application_container.feature_gates = FeatureGateSet.from_config(
        "full",
        (
            "company_model=true,company_goals=true,company_operations=true,"
            "business_objects=true,organizational_memory=true,"
            "company_finance_read=true,financial_governance=true,"
            "company_packs=true"
        ),
    )
    script = Path(__file__).parents[1] / "examples" / "market-intelligence-studio" / "run.py"
    spec = importlib.util.spec_from_file_location("market_intelligence_showcase", script)
    assert spec and spec.loader
    showcase = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(showcase)

    with TestClient(create_app(application_container)) as client:

        def local_request(method, path, payload=None):
            response = client.request(method, path, json=payload)
            assert response.status_code < 400, response.text
            return response.json()

        showcase.request = local_request
        showcase.main()

    result = json.loads(capsys.readouterr().out)
    assert result["external_writes"] is False
    assert result["draft_operation_count"] == 3
    assert result["operations_pack_digest"]
    assert result["approved_report_id"]
    assert result["claim_register_id"]
