"""Optional Feishu delivery process. Requires the feishu_notifications gate."""

import logging
import os
import time
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.config import Settings, get_settings
from agentmesh.features import Feature, FeatureGateSet
from agentmesh.integrations.feishu_identities import load_employee_bot_credentials
from agentmesh.integrations.feishu_notifications import (
    FeishuClient,
    FeishuNotificationStore,
    FeishuNotificationWorker,
)

logger = logging.getLogger(__name__)


def validate_configuration(settings: Settings) -> None:
    FeatureGateSet.from_config(settings.feature_profile, settings.feature_gates).require(
        Feature.FEISHU_NOTIFICATIONS
    )
    if not (settings.feishu_app_id or "").strip():
        raise ValueError("AGENTMESH_FEISHU_APP_ID is required")
    if settings.feishu_app_secret is None or not settings.feishu_app_secret.get_secret_value():
        raise ValueError("AGENTMESH_FEISHU_APP_SECRET is required")
    if not (settings.feishu_chat_id or "").strip():
        raise ValueError("AGENTMESH_FEISHU_CHAT_ID is required")
    if settings.feishu_task_base_url:
        url = urlsplit(settings.feishu_task_base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("AGENTMESH_FEISHU_TASK_BASE_URL must be a public HTTPS URL")
    if settings.feishu_employee_bots_enabled:
        if not settings.feishu_sync_collaboration:
            raise ValueError("Employee bots require AGENTMESH_FEISHU_SYNC_COLLABORATION")
        if not settings.feishu_employee_bots_file:
            raise ValueError("AGENTMESH_FEISHU_EMPLOYEE_BOTS_FILE is required")


def build_employee_clients(settings: Settings) -> dict[str, FeishuClient]:
    if not settings.feishu_employee_bots_enabled:
        return {}
    validate_configuration(settings)
    credentials = load_employee_bot_credentials(
        settings.feishu_employee_bots_file or "",
        tenant_id=settings.tenant_id,
        chat_id=settings.feishu_chat_id or "",
    )
    if any(bot.app_id == settings.feishu_app_id for bot in credentials.values()):
        raise ValueError("Employee bots must be distinct from the task-summary bot")
    return {
        agent_id: FeishuClient(
            app_id=bot.app_id, app_secret=bot.app_secret.get_secret_value(),
            chat_id=settings.feishu_chat_id or "", timeout_seconds=settings.feishu_timeout_seconds,
        ) for agent_id, bot in credentials.items()
    }


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    settings = get_settings()
    validate_configuration(settings)
    employee_clients = build_employee_clients(settings)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    client = FeishuClient(
        app_id=settings.feishu_app_id or "",
        app_secret=(
            settings.feishu_app_secret.get_secret_value() if settings.feishu_app_secret else ""
        ),
        chat_id=settings.feishu_chat_id or "",
        timeout_seconds=settings.feishu_timeout_seconds,
    )
    worker = FeishuNotificationWorker(
        worker_id=os.getenv("AGENTMESH_FEISHU_WORKER_ID", "feishu-1"),
        store=FeishuNotificationStore(sessions, tenant_id=settings.tenant_id),
        client=client,
        task_base_url=settings.feishu_task_base_url,
        include_content=settings.feishu_include_content,
        sync_collaboration=settings.feishu_sync_collaboration,
        employee_clients=employee_clients if settings.feishu_employee_bots_enabled else None,
        employee_bot_fallback=settings.feishu_employee_bot_fallback,
        send_interval_seconds=settings.feishu_send_interval_seconds,
    )
    try:
        while True:
            try:
                processed = worker.run_once()
            except Exception:
                logger.exception("Feishu delivery cycle failed")
                processed = 0
            if not processed:
                time.sleep(settings.feishu_scan_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        for employee_client in employee_clients.values():
            employee_client.close()
        client.close()
        engine.dispose()


if __name__ == "__main__":
    main()
