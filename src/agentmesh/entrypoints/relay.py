import json
import logging
import os
import time

from prometheus_client import start_http_server

from agentmesh.bootstrap import RelayContainer, build_relay_container
from agentmesh.config import get_settings
from agentmesh.maintenance.metrics import PrometheusRetentionMetrics

logger = logging.getLogger(__name__)


def run_relay_cycle(
    container: RelayContainer,
    retention_metrics: PrometheusRetentionMetrics,
) -> int:
    published = container.relay.publish_once()
    try:
        report = container.retention.run_if_due()
    except Exception:
        try:
            retention_metrics.record_failure()
        except Exception:
            logger.exception("Could not record the retention failure metric")
        logger.exception("Messaging retention maintenance failed")
    else:
        if report is not None:
            try:
                retention_metrics.observe(report)
            except Exception:
                logger.exception("Could not update the retention metrics")
            logger.info(
                "Messaging retention maintenance completed: %s",
                json.dumps(report.to_dict(), separators=(",", ":"), sort_keys=True),
            )
    return published


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    relay_id = os.getenv("AGENTMESH_RELAY_ID", "relay-1")
    settings = get_settings()
    container = build_relay_container(settings, relay_id=relay_id)
    retention_metrics = PrometheusRetentionMetrics()
    metrics_server = None
    if settings.relay_metrics_enabled:
        try:
            metrics_server, _metrics_thread = start_http_server(
                port=settings.relay_metrics_port,
                addr=settings.relay_metrics_host,
                registry=retention_metrics.registry,
            )
        except OSError:
            logger.exception("Could not start the Relay metrics endpoint")
    try:
        while True:
            published = run_relay_cycle(container, retention_metrics)
            if published == 0:
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()
        container.close()


if __name__ == "__main__":
    main()
