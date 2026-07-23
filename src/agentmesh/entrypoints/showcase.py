from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agentmesh.application.showcase_services import ResearchBriefShowcaseService
from agentmesh.bootstrap import build_api_container
from agentmesh.config import get_settings
from agentmesh.infrastructure.postgres.uow import SqlAlchemyUnitOfWorkFactory


def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    uow_factory = SqlAlchemyUnitOfWorkFactory(
        sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    )
    container = build_api_container(settings)
    try:
        result = ResearchBriefShowcaseService(
            task_service=container.task_service,
            planning_service=container.planning_service,
            uow_factory=uow_factory,
            tenant_id=settings.tenant_id,
        ).create()
        print(json.dumps(result.__dict__, ensure_ascii=False))
    finally:
        container.close()
        engine.dispose()


if __name__ == "__main__":
    main()
