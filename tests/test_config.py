import pytest
from pydantic import ValidationError

from agentmesh.config import Settings, get_settings


def test_cached_settings_factory_builds_settings() -> None:
    get_settings.cache_clear()

    settings = get_settings()

    assert isinstance(settings, Settings)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_block_ms", -1),
        ("worker_pending_idle_ms", 0),
        ("run_lease_seconds", 0),
        ("run_lease_renewal_seconds", 0),
        ("relay_batch_size", 0),
        ("relay_claim_seconds", 0),
        ("relay_retry_seconds", -1),
        ("retention_interval_seconds", 0),
        ("retention_batch_size", 0),
        ("outbox_retention_seconds", 0),
        ("inbox_retention_seconds", 0),
        ("redis_stream_retention_seconds", 0),
        ("redis_stream_max_entries", 0),
        ("dead_letter_stream_retention_seconds", 0),
        ("dead_letter_stream_max_entries", 0),
        ("relay_metrics_port", 0),
        ("artifact_max_inline_bytes", 0),
        ("mcp_workspace_timeout_seconds", 0),
        ("mcp_http_timeout_seconds", 0),
        ("mcp_discovery_ttl_seconds", 59),
        ("mcp_discovery_max_tools", 0),
        ("mcp_workspace_max_bytes", 0),
        ("mcp_max_result_bytes", 0),
        ("coordinated_max_concurrency", 0),
        ("a2a_reconciliation_batch_size", 0),
        ("a2a_reconciliation_scan_seconds", 0),
        ("a2a_poll_interval_seconds", 0),
        ("a2a_poll_lease_seconds", 1),
        ("a2a_poll_failure_base_seconds", 0),
        ("a2a_poll_failure_max_seconds", 0),
        ("a2a_poll_max_failures", 0),
    ],
)
def test_settings_reject_invalid_operational_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(**{field: value})

    assert field in str(exc_info.value)


def test_settings_accept_zero_for_nonnegative_delays() -> None:
    settings = Settings(worker_block_ms=0, relay_retry_seconds=0)

    assert settings.worker_block_ms == 0
    assert settings.relay_retry_seconds == 0


def test_empty_optional_credential_workload_principal_is_none() -> None:
    settings = Settings(credential_workload_principal_id="")

    assert settings.credential_workload_principal_id is None


def test_settings_requires_distinct_execution_agent_roles() -> None:
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(supervisor_agent_id="demo-agent")


def test_settings_rejects_unsafe_a2a_reconciliation_timing() -> None:
    with pytest.raises(ValidationError, match="a2a_poll_lease_seconds"):
        Settings(a2a_timeout_seconds=30, a2a_poll_lease_seconds=30)
    with pytest.raises(ValidationError, match="a2a_poll_failure_max_seconds"):
        Settings(a2a_poll_failure_base_seconds=10, a2a_poll_failure_max_seconds=5)


@pytest.mark.parametrize(
    "updates",
    [
        {
            "redis_stream_retention_seconds": 101,
            "outbox_retention_seconds": 100,
            "inbox_retention_seconds": 200,
            "dead_letter_stream_retention_seconds": 200,
        },
        {
            "redis_stream_retention_seconds": 100,
            "outbox_retention_seconds": 101,
            "inbox_retention_seconds": 100,
            "dead_letter_stream_retention_seconds": 100,
        },
        {
            "redis_stream_retention_seconds": 100,
            "outbox_retention_seconds": 100,
            "inbox_retention_seconds": 100,
            "dead_letter_stream_retention_seconds": 101,
        },
    ],
)
def test_settings_reject_unsafe_retention_horizons(updates: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="retention_seconds"):
        Settings(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {"retention_batch_size": 10_001},
        {"relay_metrics_port": 65_536},
        {"domain_event_stream": "agentmesh.run-requests"},
        {"dead_letter_stream": "agentmesh.domain-events"},
    ],
)
def test_settings_reject_unsafe_retention_capacity_or_stream_topology(
    updates: dict[str, int | str],
) -> None:
    with pytest.raises(ValidationError):
        Settings(**updates)
