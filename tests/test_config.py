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
        ("artifact_max_inline_bytes", 0),
        ("mcp_workspace_timeout_seconds", 0),
        ("mcp_workspace_max_bytes", 0),
        ("mcp_max_result_bytes", 0),
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
