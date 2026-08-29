from fastapi import FastAPI

from .routes import router

app = FastAPI(title="Fixture Payments API (Python)")
app.include_router(router, prefix="/v1")
