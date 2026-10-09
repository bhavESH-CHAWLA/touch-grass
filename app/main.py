from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .planner import build_plan

app = FastAPI(title="Touch Grass Planner")


class PlanRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    minutes: int = Field(default=30, ge=10, le=90)


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/plan")
async def plan(req: PlanRequest):
    return await build_plan(req.lat, req.lon, req.minutes)


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True), name="static")
