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
    def fake_run_agent(task, on_event, max_steps, *, planner_model, coder_model, on_workspace):
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
