import logging
import os
import socket

from agentmesh.bootstrap import build_worker_container


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    worker_id = os.getenv("AGENTMESH_WORKER_ID", f"worker-{socket.gethostname()}")
    container = build_worker_container(worker_id=worker_id)
    try:
        while True:
            container.worker.run_once()
    except KeyboardInterrupt:
        pass
    finally:
        container.close()


if __name__ == "__main__":
    main()
