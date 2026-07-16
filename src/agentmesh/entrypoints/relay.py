import logging
import os
import time

from agentmesh.bootstrap import build_relay_container


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    relay_id = os.getenv("AGENTMESH_RELAY_ID", "relay-1")
    container = build_relay_container(relay_id=relay_id)
    try:
        while True:
            published = container.relay.publish_once()
            if published == 0:
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        container.close()


if __name__ == "__main__":
    main()
