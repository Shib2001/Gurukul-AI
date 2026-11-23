# Gurukul AI Service

The AI service is an internal FastAPI service. For the first integration
slice, the backend calls it over authenticated HTTP and receives one JSON
lesson response. The local model provider defaults to `fake` so tests do not
make paid requests.

## Local setup

1. Start the backend's local Redis service from `C:\Users\shivj\Gurukul-Be`:

   ```powershell
   docker compose up -d redis
   ```

2. From this repository, create a local environment file if you do not already
   have one:

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   ```

3. Set `AI_SERVICE_AUTH_KEY` in this `.env` to the same locally generated
   service secret used by the backend. Keep the existing `.env` private and
   ignored by Git.

4. Install the locked dependencies and run tests:

   ```powershell
   uv sync --locked
   uv run pytest
   uv run ruff check src tests
   ```

5. Start the service:

   ```powershell
   uv run uvicorn gurukul_ai.main:app --host 127.0.0.1 --port 8001 --reload
   ```

6. Check service liveness in another PowerShell window:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8001/internal/v1/health/live
   ```

Use the fake provider for the first backend-to-AI contract test. To enable a
real OpenRouter call later, set `AI_PROVIDER=openrouter`, configure a newly
issued `OPENROUTER_API_KEY`, then restart the service. This test slice selects
`openai/gpt-4o-mini` in code; no model setting is required in `.env`. Never use
a revoked key or put provider credentials in the backend or frontend.