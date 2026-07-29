import logging
import os
import time

from agentmesh.bootstrap import build_api_container
from agentmesh.features import Feature
from agentmesh.workers.company_operations import CompanyOperationsWorker


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logger = logging.getLogger(__name__)
    container = build_api_container()
    container.feature_gates.require(Feature.COMPANY_OPERATIONS)
    worker = CompanyOperationsWorker(
        service=container.company_operation_service,
        batch_size=int(os.getenv("AGENTMESH_OPERATIONS_BATCH_SIZE", "50")),
    )
    scan_seconds = int(os.getenv("AGENTMESH_OPERATIONS_SCAN_SECONDS", "5"))
    try:
        while True:
            try:
                launches = worker.run_once()
            except Exception:
                logger.exception("Company Operations dispatch cycle failed")
                time.sleep(scan_seconds)
                continue
            if launches:
                logger.info("Company Operations dispatched %d occurrences", len(launches))
            else:
                time.sleep(scan_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        container.close()


if __name__ == "__main__":
    main()
