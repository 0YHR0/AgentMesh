FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
# Install third-party dependencies in a layer that remains cached when application
# source or Console assets change. The final project wheel is installed below.
COPY src/agentmesh/__init__.py ./src/agentmesh/__init__.py
RUN pip install --upgrade pip && pip install ".[observability]"

COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-deps --force-reinstall .

EXPOSE 8000

CMD ["uvicorn", "agentmesh.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
