import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Protocol

from redis.asyncio import Redis

from gurukul_ai.providers import ProviderFailure
from gurukul_ai.schemas import AIChatRunRequest, AIChatRunResponse, AIErrorResponse

IDEMPOTENCY_TTL_SECONDS = 86_400


class RunStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int) -> None: ...

    async def acquire(self, key: str, value: str, ttl: int) -> bool: ...

    async def release(self, key: str) -> None: ...

    async def ping(self) -> bool: ...


class RedisRunStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, key: str) -> str | None:
        value = await self._redis.get(key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._redis.set(key, value, ex=ttl)

    async def acquire(self, key: str, value: str, ttl: int) -> bool:
        return bool(await self._redis.set(key, value, ex=ttl, nx=True))

    async def release(self, key: str) -> None:
        await self._redis.delete(key)

    async def ping(self) -> bool:
        return bool(await self._redis.ping())


def _cache_record(
    request_hash: str,
    response: AIChatRunResponse | None = None,
    error: AIErrorResponse | None = None,
) -> str:
    record: dict[str, object] = {"request_hash": request_hash}
    if response is not None:
        record["response"] = response.model_dump(mode="json")
    if error is not None:
        record["error"] = error.model_dump(mode="json")
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


async def _cached_result(
    store: RunStore,
    cache_key: str,
    request_hash: str,
) -> AIChatRunResponse | AIErrorResponse | None:
    value = await store.get(cache_key)
    if value is None:
        return None
    try:
        record = json.loads(value)
        if record["request_hash"] != request_hash:
            raise ProviderFailure(
                "AI_INVALID_REQUEST",
                "The idempotency key was already used for a different request.",
                False,
                409,
            )
        if "response" in record:
            return AIChatRunResponse.model_validate(record["response"])
        if "error" in record:
            return AIErrorResponse.model_validate(record["error"])
    except ProviderFailure:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderFailure(
            "AI_INTERNAL_ERROR",
            "The saved idempotency result is invalid.",
            False,
            502,
        ) from error
    raise ProviderFailure(
        "AI_INTERNAL_ERROR",
        "The saved idempotency result is incomplete.",
        False,
        502,
    )


async def run_idempotently(
    request: AIChatRunRequest,
    store: RunStore,
    execute: Callable[[], Awaitable[AIChatRunResponse]],
    ttl: int = IDEMPOTENCY_TTL_SECONDS,
    namespace: str = "development",
) -> AIChatRunResponse:
    normalized = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(normalized.encode()).hexdigest()
    idempotency_hash = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
    cache_key = f"gurukul-ai:{namespace}:idempotency:{idempotency_hash}"
    lock_key = f"{cache_key}:lock"

    cached = await _cached_result(store, cache_key, request_hash)
    if isinstance(cached, AIErrorResponse):
        raise ProviderFailure(
            cached.error.code,
            cached.error.message,
            cached.error.retryable,
            _status_for_error(cached.error.code),
        )
    if cached is not None:
        return cached

    lock_ttl = max(30, request.limits.deadline_ms // 1000 + 10)
    if not await store.acquire(lock_key, request_hash, lock_ttl):
        wait_seconds = request.limits.deadline_ms / 1000
        elapsed = 0.0
        while elapsed < wait_seconds:
            await asyncio.sleep(0.1)
            elapsed += 0.1
            cached = await _cached_result(store, cache_key, request_hash)
            if isinstance(cached, AIErrorResponse):
                raise ProviderFailure(
                    cached.error.code,
                    cached.error.message,
                    cached.error.retryable,
                    _status_for_error(cached.error.code),
                )
            if cached is not None:
                return cached
        raise ProviderFailure(
            "AI_PROVIDER_TIMEOUT",
            "The matching AI request is still in progress.",
            True,
            504,
        )

    try:
        result = await execute()
    except ProviderFailure as error:
        error_response = AIErrorResponse(
            error={
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "request_id": request.request_id,
            }
        )
        await store.set(
            cache_key,
            _cache_record(request_hash, error=error_response),
            ttl,
        )
        raise
    else:
        await store.set(cache_key, _cache_record(request_hash, response=result), ttl)
        return result
    finally:
        await store.release(lock_key)


def _status_for_error(code: str) -> int:
    return {
        "AI_PROVIDER_TIMEOUT": 504,
        "AI_PROVIDER_UNAVAILABLE": 503,
        "AI_POLICY_BLOCKED": 422,
        "AI_INVALID_REQUEST": 409,
        "AI_INTERNAL_ERROR": 502,
    }[code]
