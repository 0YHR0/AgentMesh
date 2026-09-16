import logging
import os
import socket

from agentmesh.bootstrap import build_worker_container

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    worker_id = os.getenv("AGENTMESH_WORKER_ID", f"worker-{socket.gethostname()}")
    container = build_worker_container(worker_id=worker_id)
    try:
        while True:
            container.worker.run_once()
            # Deadline recovery is one bounded, optional pass after each
            # Redis delivery cycle.  The container leaves it absent for every
            # shipped profile, and the consumer itself never holds a UoW while
            # calling the Runtime adapter.
            try:
                container.process_coordinated_deadline_once()
            except Exception:
                logger.exception("Coordinated Runtime deadline recovery pass failed")
    except KeyboardInterrupt:
        pass
    finally:
        container.close()


if __name__ == "__main__":
    main()
