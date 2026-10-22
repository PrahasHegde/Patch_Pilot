import asyncio
import difflib
import io
import json
import logging
import os
import queue
import re
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import agent
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

app = FastAPI(title="PatchPilot API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


@dataclass
class AgentRun:
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    workdir: Path | None = None
    source_workdir: Path | None = None
    project_name: str | None = None
    stats: dict[str, Any] | None = None


runs: dict[str, AgentRun] = {}
projects: dict[str, "ImportedProject"] = {}
runs_lock = threading.Lock()
MAX_RETAINED_RUNS = 30
MAX_RETAINED_PROJECTS = 10
MAX_UPLOAD_BYTES = 10_000_000
MAX_PROJECT_FILES = 200
MAX_DIFF_BYTES = 1_000_000
MAX_DIFF_FILE_BYTES = 150_000


@dataclass
class ImportedProject:
    name: str
    workdir: Path
    file_count: int


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    planner_model: str = Field(min_length=1, max_length=200)
    coder_model: str = Field(min_length=1, max_length=200)
    max_steps: int = Field(default=30, ge=1, le=30)
    project_id: str | None = None


def _excluded_path(relative: str) -> bool:
    return agent.is_excluded_path(relative)


def _archive_parts(name: str) -> tuple[str, ...]:
    normalized = name.replace("\\", "/")
    windows_path = PureWindowsPath(name)
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or windows_path.is_absolute()
        or windows_path.drive
        or any(":" in part for part in path.parts)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"Unsafe project archive path: {name!r}")
    return path.parts


def _remove_temp_project(workdir: Path) -> None:
    try:
        shutil.rmtree(workdir)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("Could not remove temporary project workspace")
        raise


def _extract_project_archive(contents: bytes, name: str) -> ImportedProject:
    if not contents:
        raise ValueError("The uploaded project archive is empty.")
    project_dir = Path(tempfile.mkdtemp(prefix="patchpilot_project_"))
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
            seen: set[str] = set()
            total_size = 0
            archive_infos = archive.infolist()
            if len(archive_infos) > MAX_PROJECT_FILES * 2:
                raise ValueError(f"Project archives are limited to {MAX_PROJECT_FILES * 2} total entries.")
            for info in archive_infos:
                parts = _archive_parts(info.filename.rstrip("/"))
                if not parts:
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError(f"Symbolic links are not allowed: {info.filename!r}")
                if info.flag_bits & 1:
                    raise ValueError("Encrypted project archives are not supported.")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError("Project archives must use ZIP or deflate compression.")
                if info.is_dir():
                    continue
                key = "/".join(parts).casefold()
                if key in seen:
                    raise ValueError(f"Duplicate project archive path: {info.filename!r}")
                seen.add(key)
                entries.append((info, parts))
                if len(entries) > MAX_PROJECT_FILES:
                    raise ValueError(f"Project archives are limited to {MAX_PROJECT_FILES} files.")
                total_size += info.file_size
                if total_size > agent.MAX_WORKSPACE_BYTES:
                    raise ValueError("The extracted project exceeds the 20 MB workspace limit.")

            if not entries:
                raise ValueError("The project archive contains no files.")

            roots = [parts[0] for _, parts in entries]
            common_root = roots[0] if all(root == roots[0] for root in roots) else None
            if common_root and all(len(parts) > 1 for _, parts in entries):
                entries = [(info, parts[1:]) for info, parts in entries]

            written = 0
            for info, parts in entries:
                relative = PurePosixPath(*parts).as_posix()
                if _excluded_path(relative):
                    continue
                destination = (project_dir / Path(*parts)).resolve()
                try:
                    destination.relative_to(project_dir.resolve())
                except ValueError as exc:
                    raise ValueError(f"Project archive path escapes the workspace: {info.filename!r}") from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with archive.open(info) as source, destination.open("wb") as target:
                    while chunk := source.read(64 * 1024):
                        size += len(chunk)
                        if size > info.file_size or size > agent.MAX_WORKSPACE_BYTES:
                            raise ValueError(f"Invalid or oversized project file: {info.filename!r}")
                        target.write(chunk)
                if size != info.file_size:
                    raise ValueError(f"Project archive file size did not match: {info.filename!r}")
                written += 1

        if written == 0:
            raise ValueError("The archive contains no supported project files after excluding caches and secrets.")
        return ImportedProject(name=name, workdir=project_dir, file_count=written)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zlib.error) as exc:
        _remove_temp_project(project_dir)
        raise ValueError(f"Could not read the project ZIP: {exc}") from exc
    except ValueError:
        _remove_temp_project(project_dir)
        raise


def _release_project_if_unused(workdir: Path) -> None:
    if any(project.workdir == workdir for project in projects.values()):
        return
    if any(run.source_workdir == workdir for run in runs.values()):
        return
    _remove_temp_project(workdir)


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
            project_source=run.source_workdir,
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


@app.post("/api/projects", status_code=201)
async def upload_project(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not filename.lower().endswith(".zip"):
        raise HTTPException(status_code=415, detail="Upload a ZIP archive of your project.")
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Project ZIP files are limited to 10 MB.")
    try:
        project = _extract_project_archive(contents, Path(filename).stem or "project")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    project_id = uuid.uuid4().hex
    with runs_lock:
        retained_projects = {project.workdir for project in projects.values()}
        retained_projects.update(
            run.source_workdir for run in runs.values() if run.source_workdir is not None
        )
        if len(retained_projects) >= MAX_RETAINED_PROJECTS:
            _remove_temp_project(project.workdir)
            raise HTTPException(
                status_code=429,
                detail="The server has reached its temporary project limit. Remove an unused project or restart the app to clear completed runs.",
            )
        projects[project_id] = project
    return {"project_id": project_id, "name": project.name, "file_count": project.file_count}


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: str) -> Response:
    with runs_lock:
        project = projects.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Imported project not found.")
        projects.pop(project_id)
        _release_project_if_unused(project.workdir)
    return Response(status_code=204)


@app.post("/api/runs", status_code=202)
def create_run(request: RunRequest) -> dict[str, str]:
    if not request.task.strip():
        raise HTTPException(status_code=422, detail="Describe the coding task before starting.")
    run_id = uuid.uuid4().hex
    with runs_lock:
        project = projects.get(request.project_id) if request.project_id else None
        if request.project_id and project is None:
            raise HTTPException(status_code=404, detail="Imported project not found. Upload it again before starting a run.")
        run = AgentRun(
            source_workdir=project.workdir if project else None,
            project_name=project.name if project else None,
        )
        if len(runs) >= MAX_RETAINED_RUNS:
            completed_ids = [
                existing_id for existing_id, existing_run in runs.items()
                if existing_run.stats is not None
            ]
            while len(runs) >= MAX_RETAINED_RUNS and completed_ids:
                removed = runs.pop(completed_ids.pop(0), None)
                if removed and removed.source_workdir is not None:
                    _release_project_if_unused(removed.source_workdir)
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
        relative = path.relative_to(run.workdir).as_posix()
        if not path.is_file() or path.is_symlink() or _excluded_path(relative):
            continue
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
    if _excluded_path(path.relative_to(run.workdir.resolve()).as_posix()):
        raise HTTPException(status_code=404, detail="File not found.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if path.stat().st_size > 150_000:
        raise HTTPException(status_code=413, detail="File is too large to preview.")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="This file cannot be previewed as text.") from exc
    return {"path": path.relative_to(run.workdir).as_posix(), "content": content}


@app.get("/api/runs/{run_id}/diff")
def run_diff(run_id: str) -> dict[str, Any]:
    run = _get_run(run_id)
    if run.workdir is None:
        raise HTTPException(status_code=409, detail="The run has not produced files yet.")
    original = run.source_workdir
    before = {
        path.relative_to(original).as_posix(): path
        for path in original.rglob("*")
        if original is not None and path.is_file() and not path.is_symlink()
        and not _excluded_path(path.relative_to(original).as_posix())
    } if original is not None else {}
    after = {
        path.relative_to(run.workdir).as_posix(): path
        for path in run.workdir.rglob("*")
        if path.is_file() and not path.is_symlink()
        and not _excluded_path(path.relative_to(run.workdir).as_posix())
    }
    changes: list[dict[str, str]] = []
    total_diff_bytes = 0
    truncated = False
    for relative in sorted(before.keys() | after.keys()):
        old_path = before.get(relative)
        new_path = after.get(relative)
        status = "added" if old_path is None else "deleted" if new_path is None else "modified"
        if old_path is not None and new_path is not None:
            if old_path.stat().st_size == new_path.stat().st_size and old_path.read_bytes() == new_path.read_bytes():
                continue
        if len(changes) >= MAX_PROJECT_FILES:
            truncated = True
            break
        if (old_path and old_path.stat().st_size > MAX_DIFF_FILE_BYTES) or (
            new_path and new_path.stat().st_size > MAX_DIFF_FILE_BYTES
        ):
            diff = "Diff omitted because the file exceeds the 150 KB review limit."
        else:
            try:
                old_lines = old_path.read_text(encoding="utf-8").splitlines() if old_path else []
                new_lines = new_path.read_text(encoding="utf-8").splitlines() if new_path else []
                diff = "\n".join(difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile=f"a/{relative}" if old_path else "/dev/null",
                    tofile=f"b/{relative}" if new_path else "/dev/null",
                    lineterm="",
                ))
            except UnicodeDecodeError:
                diff = "Diff omitted because this is a binary or non-UTF-8 file."
        encoded_size = len(diff.encode("utf-8"))
        if total_diff_bytes + encoded_size > MAX_DIFF_BYTES:
            truncated = True
            break
        total_diff_bytes += encoded_size
        changes.append({"path": relative, "status": status, "diff": diff})
    return {"changes": changes, "truncated": truncated}


@app.get("/api/runs/{run_id}/download")
def download_run(run_id: str) -> Response:
    run = _get_run(run_id)
    if run.workdir is None:
        raise HTTPException(status_code=409, detail="The run has not produced files yet.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(run.workdir.rglob("*")):
            if path.is_file() and not path.is_symlink() and not _excluded_path(path.relative_to(run.workdir).as_posix()):
                archive.write(path, path.relative_to(run.workdir).as_posix())
    archive_name = re.sub(r"[^A-Za-z0-9._-]+", "_", run.project_name or "patchpilot-output").strip("._")
    archive_name = archive_name or "patchpilot-output"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{archive_name}-patched.zip"'},
    )


frontend_dist = Path(__file__).parent / "frontend" / "dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
