"""Collaboration egress requires both explicit notification and detail opt-ins."""

import pytest
from pydantic import ValidationError

from agentmesh import bootstrap
from agentmesh.config import Settings


@pytest.mark.parametrize(
    ("gates", "requested", "enabled"),
    [
        ("", False, False),
        ("", True, False),
        ("feishu_notifications=true", False, False),
        ("feishu_notifications=true", True, True),
    ],
)
def test_collaboration_queue_requires_parent_gate(monkeypatch, gates, requested, enabled):
    captured = {}
    monkeypatch.setattr(bootstrap, "create_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(bootstrap, "sessionmaker", lambda **kwargs: object())

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(bootstrap, "SqlAlchemyUnitOfWorkFactory", factory)
    bootstrap._database_components(
        Settings(_env_file=None, feature_gates=gates, feishu_sync_collaboration=requested)
    )
    assert captured["feishu_collaboration_enabled"] is enabled


def test_collaboration_settings_default_to_no_content_egress():
    settings = Settings(_env_file=None)
    assert settings.feishu_sync_collaboration is False
    assert settings.feishu_include_content is False
    assert settings.feishu_send_interval_seconds == 1.0


@pytest.mark.parametrize("value", [0, -1, 11, float("nan"), float("inf")])
def test_notification_pacing_is_bounded(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, feishu_send_interval_seconds=value)
