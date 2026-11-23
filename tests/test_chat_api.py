import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from gurukul_ai.config import Settings
from gurukul_ai.main import create_app
from gurukul_ai.providers import (
    OPENROUTER_MODEL,
    FakeLessonProvider,
    OpenRouterLessonProvider,
    ProviderResult,
)
from gurukul_ai.schemas import AIChatRunRequest


class MemoryRunStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.locks: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, _ttl: int) -> None:
        self.values[key] = value
        self.expirations[key] = _ttl

    async def acquire(self, key: str, _value: str, _ttl: int) -> bool:
        if key in self.locks:
            return False
        self.locks[key] = _value
        self.expirations[key] = _ttl
        return True

    async def release(self, key: str) -> None:
        self.locks.pop(key, None)

    async def ping(self) -> bool:
        return True


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: AIChatRunRequest) -> ProviderResult:
        self.calls += 1
        return await FakeLessonProvider().generate(request)


def _request(operation: str = "lesson_start") -> dict[str, object]:
    request: dict[str, object] = {
        "contract_version": "2026-01",
        "request_id": f"req_{uuid4().hex}",
        "idempotency_key": f"conversation:{uuid4()}",
        "operation": operation,
        "user_ref": "opaque-user",
        "conversation_ref": "opaque-conversation",
        "teacher_bot_id": "teacher_math",
        "subject_id": "subject_math",
        "student_context": {
            "grade_level": "5",
            "language": "en",
            "learning_preferences": [],
        },
        "messages": [],
        "limits": {"max_output_tokens": 700, "deadline_ms": 20_000},
        "policy_versions": {
            "safety": "1",
            "teacher": "math-3",
            "retrieval": "2",
        },
    }
    if operation == "lesson_turn":
        request["messages"] = [
            {
                "role": "assistant",
                "content": "What fraction is one slice out of four?",
                "created_at": datetime.now(UTC).isoformat(),
            },
            {
                "role": "user",
                "content": "One fourth.",
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        request["lesson_state"] = {
            "topic": "Fractions",
            "phase": "checking_understanding",
            "understanding": "not_assessed",
            "next_action": "ask_question",
        }
    return request


def _client(
    provider: CountingProvider | None = None,
) -> tuple[TestClient, CountingProvider]:
    model_provider = provider or CountingProvider()
    app = create_app(
        settings=Settings(
            AI_SERVICE_AUTH_KEY="test-shared-secret",
            AI_PROVIDER="fake",
        ),
        provider=model_provider,
        run_store=MemoryRunStore(),
    )
    return TestClient(app), model_provider


def _headers(payload: dict[str, object]) -> dict[str, str]:
    return {
        "Authorization": "Bearer test-shared-secret",
        "X-Request-ID": str(payload["request_id"]),
        "Idempotency-Key": str(payload["idempotency_key"]),
    }


def test_lesson_start_matches_backend_json_contract() -> None:
    payload = _request()
    client, provider = _client()

    with client:
        response = client.post(
            "/internal/v1/chat/runs",
            json=payload,
            headers=_headers(payload),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "2026-01"
    assert body["status"] == "completed"
    assert body["content"]["text"]
    assert body["lesson_progress"] == {
        "topic": "Fractions",
        "phase": "checking_understanding",
        "understanding": "not_assessed",
        "next_action": "ask_question",
    }
    assert provider.calls == 1


def test_lesson_turn_uses_saved_lesson_state_contract() -> None:
    payload = _request("lesson_turn")
    client, _ = _client()

    with client:
        response = client.post(
            "/internal/v1/chat/runs",
            json=payload,
            headers=_headers(payload),
        )

    assert response.status_code == 200
    assert response.json()["lesson_progress"]["topic"] == "Fractions"


def test_matching_idempotent_retry_reuses_exact_response() -> None:
    payload = _request()
    client, provider = _client()

    with client:
        first = client.post(
            "/internal/v1/chat/runs",
            json=payload,
            headers=_headers(payload),
        )
        second = client.post(
            "/internal/v1/chat/runs",
            json=payload,
            headers=_headers(payload),
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert provider.calls == 1


def test_idempotency_key_cannot_be_reused_for_different_request() -> None:
    payload = _request()
    changed = {**payload, "request_id": f"req_{uuid4().hex}"}
    client, provider = _client()

    with client:
        first = client.post(
            "/internal/v1/chat/runs",
            json=payload,
            headers=_headers(payload),
        )
        second = client.post(
            "/internal/v1/chat/runs",
            json=changed,
            headers=_headers(changed),
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "AI_INVALID_REQUEST"
    assert provider.calls == 1


def test_backend_authentication_is_required() -> None:
    payload = _request()
    client, _ = _client()
    headers = _headers(payload)
    headers["Authorization"] = "Bearer wrong-secret"

    with client:
        response = client.post(
            "/internal/v1/chat/runs",
            json=payload,
            headers=headers,
        )

    assert response.status_code == 401


def test_openrouter_provider_uses_code_selected_model_without_model_env() -> None:
    payload = AIChatRunRequest.model_validate(_request())
    observed: dict[str, object] = {}

    def handle_request(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed["model"] = body["model"]
        observed["authorization"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "text": "What fraction is one slice out of four?",
                                    "topic": "Fractions",
                                    "phase": "checking_understanding",
                                    "understanding": "not_assessed",
                                    "next_action": "ask_question",
                                    "safety_status": "allowed",
                                    "safety_flags": [],
                                }
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 25,
                    "completion_tokens": 18,
                    "cost": 0.0001,
                },
            },
            request=request,
        )

    async def run_provider() -> ProviderResult:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle_request)
        ) as client:
            provider = OpenRouterLessonProvider(
                client,
                Settings(
                    OPENROUTER_API_KEY="test-key",
                    OPENROUTER_BASE_URL="https://router.test/v1",
                ),
            )
            return await provider.generate(payload)

    result = asyncio.run(run_provider())

    assert observed["model"] == OPENROUTER_MODEL
    assert observed["authorization"] == "Bearer test-key"
    assert result.input_tokens == 25
    assert result.output_tokens == 18
