"""Opt-in MemOS Cloud mirror for already-governed organizational memories.

This adapter never grants access to a Memory record. PostgreSQL remains the
authority; MemOS supplies only an ordering for a locally authorized set.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any
from uuid import UUID

import httpx

from agentmesh.application.organizational_memory_services import (
    PostgresExactMemoryRankingBackend,
)
from agentmesh.domain.organizational_memory import MemoryRecord, MemorySensitivity


class MemOSUnavailable(Exception):
    """The optional remote ranker cannot be used for this request."""


class MemOSCloudClient:
    BASE_URL = "https://memos.memtensor.cn/api/openmem/v1"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("MemOS API key is required")
        self._key = api_key
        self._timeout = timeout_seconds
        self._transport = transport

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.BASE_URL,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    path,
                    headers={"Authorization": f"Token {self._key}"},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MemOSUnavailable("MemOS request failed") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise MemOSUnavailable("MemOS rejected the request")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MemOSUnavailable("MemOS returned an invalid response")
        return data

    def replace_user_memories(
        self, user_id: str, records: list[MemoryRecord]
    ) -> int:
        """Rebuild only AgentMesh's dedicated remote user namespace."""
        if not user_id.startswith("agentmesh-") or len(records) > 25:
            raise ValueError("MemOS pilot scope is invalid or exceeds 25 records")
        # MemOS currently returns HTTP 500 when deleting a user with no memories.
        # A synthetic marker establishes that namespace before an explicit reset.
        marker = self._post(
            "/add/message",
            {
                "user_id": user_id,
                "conversation_id": "agentmesh-sync-marker",
                "messages": [
                    {
                        "role": "user",
                        "content": "AgentMesh sync marker; ignore this synthetic note.",
                    }
                ],
                "allow_public": False,
                "async_mode": False,
            },
        )
        if marker.get("success") is not True or marker.get("status") != "completed":
            raise MemOSUnavailable("MemOS synchronization marker was not completed")
        result = self._post("/delete/memory", {"user_id": user_id})
        if result.get("success") is not True:
            raise MemOSUnavailable("MemOS deletion was not confirmed")
        for memory in records:
            result = self._post(
                "/add/message",
                {
                    "user_id": user_id,
                    "conversation_id": f"agentmesh-{memory.id}",
                    "messages": [{"role": "user", "content": memory.content}],
                    "info": {"agentmesh_memory_id": str(memory.id)},
                    "allow_public": False,
                    "async_mode": False,
                },
            )
            if result.get("success") is not True or result.get("status") != "completed":
                raise MemOSUnavailable("MemOS memory processing was not completed")
        return len(records)

    def search_ids(self, user_id: str, query: str) -> list[UUID]:
        data = self._post(
            "/search/memory",
            {"user_id": user_id, "query": query[:2_000], "memory_limit_number": 25},
        )
        ids: list[UUID] = []
        for field in ("memory_detail_list", "preference_detail_list"):
            rows = data.get(field, [])
            if not isinstance(rows, list):
                raise MemOSUnavailable("MemOS returned invalid search results")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                conversation_id = row.get("conversation_id", "")
                if not isinstance(conversation_id, str) or not conversation_id.startswith(
                    "agentmesh-"
                ):
                    continue
                try:
                    memory_id = UUID(conversation_id.removeprefix("agentmesh-"))
                except ValueError:
                    continue
                if memory_id not in ids:
                    ids.append(memory_id)
        return ids


class MemOSMemoryRankingBackend:
    name = "memos-cloud"

    def __init__(
        self,
        *,
        client: MemOSCloudClient,
        tenant_id: str,
        allowed_company_id: UUID,
    ) -> None:
        self._client = client
        self.allowed_company_id = allowed_company_id
        self._tenant_id = tenant_id
        self._fallback = PostgresExactMemoryRankingBackend()

    def _user_id(self) -> str:
        digest = sha256(
            f"{self._tenant_id}:{self.allowed_company_id}".encode()
        ).hexdigest()[:32]
        return f"agentmesh-{digest}"

    def sync(self, records: list[MemoryRecord]) -> int:
        if any(record.company_id != self.allowed_company_id for record in records):
            raise ValueError("MemOS sync company does not match the configured pilot")
        return self._client.replace_user_memories(self._user_id(), records)

    def rank(self, query: str, candidates: list[MemoryRecord]) -> list[MemoryRecord]:
        exact = self._fallback.rank(query, candidates)
        if not candidates or any(
            item.company_id != self.allowed_company_id
            or item.sensitivity not in {MemorySensitivity.PUBLIC, MemorySensitivity.INTERNAL}
            for item in candidates
        ):
            return exact
        try:
            remote_ids = self._client.search_ids(self._user_id(), query)
        except MemOSUnavailable:
            return exact
        canonical = {item.id: item for item in exact}
        preferred = [canonical[item_id] for item_id in remote_ids if item_id in canonical]
        preferred_ids = {item.id for item in preferred}
        return preferred + [item for item in exact if item.id not in preferred_ids]
