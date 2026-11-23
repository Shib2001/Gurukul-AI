import hmac
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from gurukul_ai.cache import RedisRunStore, RunStore, run_idempotently
from gurukul_ai.config import Settings, get_settings
from gurukul_ai.graph import run_lesson_graph
from gurukul_ai.providers import (
    FakeLessonProvider,
    LessonProvider,
    OpenRouterLessonProvider,
    ProviderFailure,
    to_run_response,
)
from gurukul_ai.schemas import (
    AIChatRunRequest,
    AIChatRunResponse,
    AIErrorResponse,
)


def _error_response(
    request_id: str,
    code: str,
    message: str,
    retryable: bool,
    status_code: int,
) -> JSONResponse:
    body = AIErrorResponse(
        error={
            "code": code,
            "message": message,
            "retryable": retryable,
            "request_id": request_id,
        }
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def create_app(
    settings: Settings | None = None,
    provider: LessonProvider | None = None,
    run_store: RunStore | None = None,
) -> FastAPI:
    service_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        application.state.http_client = httpx.AsyncClient()
        redis_client: Redis | None = None
        try:
            if run_store is None:
                redis_client = Redis.from_url(
                    service_settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                application.state.run_store = RedisRunStore(redis_client)
            else:
                application.state.run_store = run_store

            if provider is not None:
                application.state.provider = provider
            elif service_settings.AI_PROVIDER == "fake":
                application.state.provider = FakeLessonProvider()
            else:
                if not service_settings.OPENROUTER_API_KEY.get_secret_value():
                    raise RuntimeError("OPENROUTER_API_KEY must be configured")
                application.state.provider = OpenRouterLessonProvider(
                    application.state.http_client,
                    service_settings,
                )
            yield
        finally:
            await application.state.http_client.aclose()
            if redis_client is not None:
                await redis_client.aclose()

    application = FastAPI(
        title="Gurukul AI Service",
        version="2026-01",
        lifespan=lifespan,
    )
    application.state.settings = service_settings

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        _exception: RequestValidationError,
    ) -> JSONResponse:
        request_id = request.headers.get("X-Request-ID", "unknown")[:80]
        return _error_response(
            request_id,
            "AI_INVALID_REQUEST",
            (
                "The AI request did not match the supported contract "
                f"({len(_exception.errors())} validation errors)."
            ),
            False,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    async def authenticate_backend(
        authorization: str | None = Header(default=None),
    ) -> None:
        expected = service_settings.AI_SERVICE_AUTH_KEY.get_secret_value()
        if not expected or expected == "replace-me":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI service authentication is not configured",
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Backend service authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Backend service authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @application.get("/internal/v1/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/internal/v1/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        try:
            ready = await request.app.state.run_store.ping()
        except (OSError, RedisError):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "dependency": "redis"},
            )
        if not ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "dependency": "redis"},
            )
        return JSONResponse(content={"status": "ready"})

    @application.post(
        "/internal/v1/chat/runs",
        response_model=AIChatRunResponse,
        dependencies=[Depends(authenticate_backend)],
    )
    async def run_chat(
        payload: AIChatRunRequest,
        request: Request,
        x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> AIChatRunResponse | JSONResponse:
        if (
            x_request_id != payload.request_id
            or idempotency_key != payload.idempotency_key
        ):
            return _error_response(
                payload.request_id,
                "AI_INVALID_REQUEST",
                "Request and idempotency headers must match the request body.",
                False,
                status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        async def execute() -> AIChatRunResponse:
            provider_result = await run_lesson_graph(
                payload,
                request.app.state.provider,
            )
            data = to_run_response(payload, provider_result)
            try:
                return AIChatRunResponse.model_validate(data)
            except ValidationError as error:
                raise ProviderFailure(
                    "AI_INTERNAL_ERROR",
                    "The AI service could not validate the lesson result.",
                    False,
                    502,
                ) from error

        try:
            store: RunStore = request.app.state.run_store
            return await run_idempotently(
                payload,
                store,
                execute,
                service_settings.AI_IDEMPOTENCY_TTL_SECONDS,
                service_settings.APP_ENV,
            )
        except ProviderFailure as error:
            return _error_response(
                payload.request_id,
                error.code,
                error.message,
                error.retryable,
                error.status_code,
            )
        except (OSError, RedisError):
            return _error_response(
                payload.request_id,
                "AI_PROVIDER_UNAVAILABLE",
                "The AI service's idempotency store is temporarily unavailable.",
                True,
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

    return application


app = create_app()
