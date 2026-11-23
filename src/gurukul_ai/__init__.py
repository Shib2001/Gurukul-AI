def main() -> None:
    import uvicorn

    uvicorn.run("gurukul_ai.main:app", host="127.0.0.1", port=8001, reload=True)
