import asyncio
import json
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import agent
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

app = FastAPI(title="PatchPilot API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@dataclass
class AgentRun:
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    workdir: Path | None = None
    stats: dict[str, Any] | None = None


runs: dict[str, AgentRun] = {}
runs_lock = threading.Lock()
MAX_RETAINED_RUNS = 30


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    planner_model: str = Field(min_length=1, max_length=200)
    coder_model: str = Field(min_length=1, max_length=200)
    max_steps: int = Field(default=8, ge=1, le=20)


def _emit(run: AgentRun, event: dict[str, Any]) -> None:
    run.events.put(event)


def _run_agent(run_id: str, run: AgentRun, request: RunRequest) -> None:
    try:
        workdir, stats = agent.run_agent(
            request.task,
            on_event=lambda event: _emit(run, event),
            max_steps=request.max_steps,
            planner_model=request.planner_model,
            coder_model=request.coder_model,
            on_workspace=lambda path: setattr(run, "workdir", path),
        )
        run.workdir = workdir
        run.stats = stats
        _emit(run, {"type": "complete", "run_id": run_id, "stats": stats})
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        run.stats = {
            "passed": False,
            "status": "failed",
            "failure_reason": message,
            "steps": 0,
            "tokens": 0,
            "seconds": 0,
        }
        logger.exception("Agent run %s failed", run_id)
        _emit(run, {"type": "error", "message": message, "stats": run.stats})


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def config() -> dict[str, Any]:
    return {
        "planner_model": agent.PLANNER,
        "coder_model": agent.CODER,
        "api_key_configured": bool(os.getenv("NEBIUS_API_KEY")),
    }


@app.post("/api/runs", status_code=202)
def create_run(request: RunRequest) -> dict[str, str]:
    if not request.task.strip():
        raise HTTPException(status_code=422, detail="Describe the coding task before starting.")
    run_id = uuid.uuid4().hex
    run = AgentRun()
    with runs_lock:
        if len(runs) >= MAX_RETAINED_RUNS:
            completed_ids = [
                existing_id for existing_id, existing_run in runs.items()
                if existing_run.stats is not None
            ]
            while len(runs) >= MAX_RETAINED_RUNS and completed_ids:
                runs.pop(completed_ids.pop(0), None)
            if len(runs) >= MAX_RETAINED_RUNS:
                raise HTTPException(
                    status_code=429,
                    detail="The run queue is full. Wait for an active run to finish, then try again.",
                )
        runs[run_id] = run
    threading.Thread(target=_run_agent, args=(run_id, run, request), daemon=True).start()
    return {"run_id": run_id}


def _get_run(run_id: str) -> AgentRun:
    with runs_lock:
        run = runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    run = _get_run(run_id)

    async def stream():
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.to_thread(run.events.get, True, 15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in {"complete", "error"}:
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}/files")
def run_files(run_id: str) -> dict[str, list[dict[str, Any]]]:
    run = _get_run(run_id)
    if run.workdir is None:
        return {"files": []}
    files = []
    for path in sorted(run.workdir.rglob("*")):
        if not path.is_file() or any(
            part in {".pytest_cache", "__pycache__"} for part in path.parts
        ):
            continue
        relative = path.relative_to(run.workdir).as_posix()
        files.append({"path": relative, "size": path.stat().st_size})
        if len(files) >= 100:
            break
    return {"files": files}


@app.get("/api/runs/{run_id}/files/{file_path:path}")
def run_file(run_id: str, file_path: str) -> dict[str, str]:
    run = _get_run(run_id)
    if run.workdir is None:
        raise HTTPException(status_code=409, detail="The run has not produced files yet.")
    try:
        path = agent.safe_path(run.workdir, file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if path.stat().st_size > 150_000:
        raise HTTPException(status_code=413, detail="File is too large to preview.")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="This file cannot be previewed as text.") from exc
    return {"path": path.relative_to(run.workdir).as_posix(), "content": content}


frontend_dist = Path(__file__).parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
