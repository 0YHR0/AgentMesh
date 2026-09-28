from pathlib import Path

CONSOLE = Path(__file__).parents[1] / "src" / "agentmesh" / "api" / "console_assets"


def test_task_modes_do_not_validate_hidden_coordinated_controls() -> None:
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")

    assert '<fieldset id="team-fields" class="hidden">' in html
    assert '$("team-fields").disabled = !coordinated;' in script
    assert '$("max-concurrency").disabled = !coordinated;' in script


def test_coordinated_defaults_and_payload_keep_shared_task_context() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")

    assert (
        'role: t("Analysis"), objective: t("Analyze the materials and develop '
        'candidate findings"), '
        'depends: ["research"]'
    ) in script
    assert 'depends: ["research"]' in script
    assert "goal: objective, materials, expected_output: expected" in script
    assert "hasDependencyCycle(subtasks)" in script
    assert "preferred_agent_id: row.querySelector(\".role-agent\").value.trim()" in script
    assert (
        '[["REVIEWED", "reviewed_execution", []], '
        '["COORDINATED", "coordinated_execution", ["agent_registry_management"]]]'
    ) in script
    assert "extra.every(featureEnabled)" in script


def test_employee_connection_selection_survives_agent_refresh() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")

    assert "const previous = select.value;" in script
    assert "if (eligible.some((item) => item.id === previous)) select.value = previous;" in script
    assert "DeepSeek requires a saved model connection." in script
    assert script.index("DeepSeek requires a saved model connection.") < script.index(
        '$("version-create-button").disabled = true;'
    )


def test_first_company_setup_does_not_require_company_packs() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")

    assert '$("open-company-setup").disabled = !featureEnabled("company_model");' in script
    assert 'api("/api/v1/companies", { method: "POST"' in script
    assert 'company-setup-form" class="company-setup-form hidden"' in html
    assert "Company packs are optional." in script
    assert "error.status = response.status;" in script
    assert 'error.status === 404 && /no active company exists/i.test(error.message)' in script


def test_provider_configuration_is_not_reported_as_execution_ready() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")

    assert 't("Connection configured — test required")' in script
    assert 't("Provider test passed · assignment still required")' in script
    assert 't("No model connection configured")' in script
    assert (
        "state.modelConnectionTests.get(connection.id).revision !== connection.revision"
        in script
    )


def test_task_preview_uses_supported_goal_contract_and_explicit_run_action() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")

    assert 'goal: { success_criteria: successCriteria }' in script
    assert "input.success_criteria = successCriteria" in script
    assert 'api(`/api/v1/tasks/${encodeURIComponent(task.id)}/runs`' in script
    assert 'id="task-review-step"' in html
    assert 'id="create-task-only"' in html and 'id="create-and-run"' in html


def test_manual_memory_notes_use_reviewed_company_policy_endpoint() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")

    assert 'memory/notes' in script
    assert 'policy_id: policyId, content:' in script
    assert 'id="manual-memory-form"' in html
    assert "server-derived provenance and evidence" in html


def test_advanced_navigation_and_capabilities_are_collapsed_by_default() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")

    assert 'id="advanced-nav" class="advanced-nav"' in html
    assert 'id="advanced-capabilities"' in html
    assert 'advanced-feature-readiness-list' in script


def test_task_review_includes_memory_and_optional_limit_choices() -> None:
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")

    assert 't("Company memory")' in script
    assert 't("Optional limits")' in script
    assert 'payload.budget?.max_runs' in script
    assert 'payload.budget?.deadline' in script


def test_task_dialog_controls_fit_narrow_viewports_without_hiding_fields() -> None:
    styles = (CONSOLE / "app.css").read_text(encoding="utf-8")

    assert (
        "#task-edit-step,#task-review-step,#execution-row{width:100%;min-width:0;max-width:100%"
        in styles
    )
    assert (
        "#task-edit-step input,#task-edit-step textarea,#task-edit-step select{width:100%;"
        "min-width:0"
        in styles
    )


def test_execution_mode_copy_is_translated() -> None:
    translations = (CONSOLE / "i18n.js").read_text(encoding="utf-8")
    script = (CONSOLE / "app.js").read_text(encoding="utf-8")

    assert (
        't(mode === "REVIEWED" ? "deterministic review policy" : '
        '"selected published employees")'
    ) in script
    assert '"deterministic review policy": "确定性审核策略"' in translations
    assert '"selected published employees": "所选已发布员工"' in translations
