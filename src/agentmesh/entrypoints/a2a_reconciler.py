import logging
import os
import socket
import time

from agentmesh.bootstrap import build_a2a_reconciler_container


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger = logging.getLogger(__name__)
    worker_id = os.getenv("AGENTMESH_A2A_RECONCILER_ID", f"a2a-{socket.gethostname()}")
    container = build_a2a_reconciler_container(worker_id=worker_id)
    try:
        while True:
            try:
                report = container.worker.run_once()
            except Exception:
                logger.exception("A2A reconciliation cycle failed; leased rows will be reclaimed")
                time.sleep(container.scan_interval_seconds)
                continue
            if report.claimed:
                logger.info("A2A reconciliation completed: %s", report)
            if report.claimed == 0:
                time.sleep(container.scan_interval_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        container.close()


if __name__ == "__main__":
    main()
