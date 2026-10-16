import io
import zipfile

from fastapi.testclient import TestClient

import app as app_module


client = TestClient(app_module.app)


def test_health_and_config_do_not_expose_api_key(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "sensitive-test-key")

    assert client.get("/api/health").json() == {"status": "ok"}
    config = client.get("/api/config").json()
    assert config["api_key_configured"] is True
    assert "sensitive-test-key" not in str(config)


def test_run_stream_and_generated_file_endpoints(monkeypatch, tmp_path):
    def fake_run_agent(task, on_event, max_steps, *, planner_model, coder_model, project_source, on_workspace):
        on_workspace(tmp_path)
        on_event({"type": "plan", "text": "Create a small module."})
        (tmp_path / "result.py").write_text("answer = 42\n", encoding="utf-8")
        on_event({"type": "write", "path": "result.py", "content": "answer = 42\n"})
        return tmp_path, {"passed": True, "steps": 1, "tokens": 12, "seconds": 0.1}

    monkeypatch.setattr(app_module.agent, "run_agent", fake_run_agent)
    response = client.post(
        "/api/runs",
        json={
            "task": "Create a small module",
            "planner_model": "planner",
            "coder_model": "coder",
            "max_steps": 2,
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    events = client.get(f"/api/runs/{run_id}/events")
    assert events.status_code == 200
    assert '"type": "plan"' in events.text
    assert '"type": "complete"' in events.text

    files = client.get(f"/api/runs/{run_id}/files").json()["files"]
    assert files == [{"path": "result.py", "size": (tmp_path / "result.py").stat().st_size}]
    preview = client.get(f"/api/runs/{run_id}/files/result.py")
    assert preview.json() == {"path": "result.py", "content": "answer = 42\n"}
    diff = client.get(f"/api/runs/{run_id}/diff")
    assert diff.json()["changes"][0]["status"] == "added"
    download = client.get(f"/api/runs/{run_id}/download")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.read("result.py").replace(b"\r\n", b"\n") == b"answer = 42\n"


def test_unknown_run_returns_404():
    response = client.get("/api/runs/not-a-run/files")
    assert response.status_code == 404


def test_agent_exception_is_reported_as_failed_run(monkeypatch):
    def fake_run_agent(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(app_module.agent, "run_agent", fake_run_agent)
    run = app_module.AgentRun()
    request = app_module.RunRequest(
        task="Build it",
        planner_model="planner",
        coder_model="coder",
    )

    app_module._run_agent("failing-run", run, request)

    assert run.stats["status"] == "failed"
    assert run.stats["failure_reason"] == "provider unavailable"
    assert run.stats["tokens"] == 0
    assert run.stats["steps"] == 0
    assert run.events.get_nowait() == {
        "type": "error",
        "message": "provider unavailable",
        "stats": run.stats,
    }


def test_upload_project_flattens_root_and_omits_secrets_and_caches():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("demo/src/app.py", "print('hello')\n")
        archive.writestr("demo/.env", "SECRET=value\n")
        archive.writestr("demo/.env.example", "NEBIUS_API_KEY=\n")
        archive.writestr("demo/.git/config", "private metadata")
        archive.writestr("demo/node_modules/lib.js", "dependency")

    response = client.post(
        "/api/projects",
        files={"file": ("demo.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 201
    project_id = response.json()["project_id"]
    project = app_module.projects[project_id]
    assert response.json()["file_count"] == 2
    assert (project.workdir / "src" / "app.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert (project.workdir / ".env.example").is_file()
    assert not (project.workdir / ".env").exists()
    assert not (project.workdir / ".git").exists()
    assert not (project.workdir / "node_modules").exists()

    removed = client.delete(f"/api/projects/{project_id}")
    assert removed.status_code == 204
    assert not project.workdir.exists()


def test_upload_project_rejects_path_traversal():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside.py", "malicious")

    response = client.post(
        "/api/projects",
        files={"file": ("bad.zip", buffer.getvalue(), "application/zip")},
    )

    assert response.status_code == 400
    assert "Unsafe project archive path" in response.json()["detail"]


def test_run_rejects_unknown_imported_project():
    response = client.post(
        "/api/runs",
        json={
            "task": "Update the project",
            "planner_model": "planner",
            "coder_model": "coder",
            "project_id": "missing-project",
        },
    )

    assert response.status_code == 404


def test_diff_compares_workspace_to_original_and_download_omits_secrets(tmp_path):
    original = tmp_path / "original"
    workspace = tmp_path / "workspace"
    original.mkdir()
    workspace.mkdir()
    (original / "keep.py").write_text("value = 1\n", encoding="utf-8")
    (original / "delete.py").write_text("remove me\n", encoding="utf-8")
    (original / ".env").write_text("SECRET=original\n", encoding="utf-8")
    (workspace / "keep.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "new.py").write_text("value = 3\n", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=changed\n", encoding="utf-8")
    run_id = "diff-test"
    with app_module.runs_lock:
        app_module.runs[run_id] = app_module.AgentRun(
            workdir=workspace,
            source_workdir=original,
            project_name="demo",
        )
    try:
        diff = client.get(f"/api/runs/{run_id}/diff").json()
        assert [(item["path"], item["status"]) for item in diff["changes"]] == [
            ("delete.py", "deleted"),
            ("keep.py", "modified"),
            ("new.py", "added"),
        ]
        assert "SECRET" not in str(diff)

        download = client.get(f"/api/runs/{run_id}/download")
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert set(archive.namelist()) == {"keep.py", "new.py"}
    finally:
        with app_module.runs_lock:
            app_module.runs.pop(run_id, None)


def test_removing_project_keeps_snapshot_while_a_retained_run_uses_it(tmp_path):
    project_dir = tmp_path / "snapshot"
    project_dir.mkdir()
    (project_dir / "main.py").write_text("answer = 42\n", encoding="utf-8")
    project_id = "retained-project"
    run_id = "retained-project-run"
    with app_module.runs_lock:
        app_module.projects[project_id] = app_module.ImportedProject("snapshot", project_dir, 1)
        app_module.runs[run_id] = app_module.AgentRun(source_workdir=project_dir)

    try:
        response = client.delete(f"/api/projects/{project_id}")
        assert response.status_code == 204
        assert project_dir.is_dir()
    finally:
        with app_module.runs_lock:
            app_module.runs.pop(run_id, None)
            app_module._release_project_if_unused(project_dir)
    assert not project_dir.exists()


def test_file_preview_rejects_paths_outside_run(tmp_path):
    run_id = "path-check"
    run = app_module.AgentRun(workdir=tmp_path)
    with app_module.runs_lock:
        app_module.runs[run_id] = run
    try:
        response = client.get(f"/api/runs/{run_id}/files/%2E%2E%2Fsecret.txt")
        assert response.status_code in {400, 404}
    finally:
        with app_module.runs_lock:
            app_module.runs.pop(run_id, None)
