#!/usr/bin/env bash
set -euo pipefail

export AGENTMESH_FEATURE_PROFILE="${AGENTMESH_FEATURE_PROFILE:-full}"

echo "Starting the AgentMesh Compose stack"
docker compose up -d --build

python_bin="${PYTHON_BIN:-}"
if [[ -z "${python_bin}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  else
    python_bin="python"
  fi
fi

"${python_bin}" scripts/ci/compose_e2e.py
