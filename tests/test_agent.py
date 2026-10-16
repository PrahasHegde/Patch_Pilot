import subprocess
from types import SimpleNamespace
from pathlib import Path

import agent


def test_safe_path_rejects_sibling_prefix_escape(tmp_path):
    workspace = tmp_path / "work"
    sibling = tmp_path / "workspace-escape"
    workspace.mkdir()
    sibling.mkdir()

    try:
        agent.safe_path(workspace, "../workspace-escape/file.py")
    except ValueError as exc:
        assert str(exc) == "path escapes workspace"
    else:
        raise AssertionError("Expected path outside workspace to be rejected")


def test_write_file_reports_file_directory_name_conflict(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "travel_tours_api").write_text("existing file", encoding="utf-8")

    try:
        agent.write_file(workspace, "travel_tours_api/main.py", "print('hello')")
    except ValueError as exc:
        assert "already exists as a file" in str(exc)
        assert (workspace / "travel_tours_api").read_text(encoding="utf-8") == "existing file"
    else:
        raise AssertionError("Expected file/directory conflict to be reported")


def test_write_file_creates_new_nested_file(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()

    written = agent.write_file(workspace, "travel_tours_api/main.py", "print('hello')")

    assert written == workspace / "travel_tours_api" / "main.py"
    assert written.read_text(encoding="utf-8") == "print('hello')"


def test_agent_inspects_project_files_without_exposing_secrets(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "main.py").write_text("answer = 42\n", encoding="utf-8")
    (workspace / ".env").write_text("NEBIUS_API_KEY=private\n", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dependency.js").write_text("private", encoding="utf-8")

    assert agent.list_files(workspace) == "main.py (13 bytes)"
    assert agent.read_file(workspace, "main.py") == "answer = 42\n"
    try:
        agent.read_file(workspace, ".env")
    except ValueError as exc:
        assert "excluded" in str(exc)
    else:
        raise AssertionError("Expected sensitive project files to be inaccessible")
    try:
        agent.write_file(workspace, ".env", "NEBIUS_API_KEY=generated\n")
    except ValueError as exc:
        assert "secret" in str(exc)
    else:
        raise AssertionError("Expected secret-file writes to be rejected")


def test_run_agent_copies_uploaded_project_and_includes_its_file_index(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("def answer(): return 42\n", encoding="utf-8")
    workspace = tmp_path / "run"
    workspace.mkdir()
    monkeypatch.setattr(agent.tempfile, "mkdtemp", lambda prefix: str(workspace))
    monkeypatch.setattr(agent, "run_tests", lambda workdir: ("1 passed", True))
    requests = []

    def completion_create(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            message = SimpleNamespace(content='{"objective":"Update project","steps":["Implement"]}')
        else:
            tool_call = SimpleNamespace(
                id="finish",
                function=SimpleNamespace(name="finish", arguments='{"summary":"Done"}'),
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                model_dump=lambda **options: {"role": "assistant", "tool_calls": []},
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=1),
        )

    fake_client = SimpleNamespace(
        api_key="configured",
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion_create)),
    )
    monkeypatch.setattr(agent, "client", fake_client)

    workdir, stats = agent.run_agent(
        "Update the answer function",
        on_event=lambda event: None,
        project_source=source,
    )

    assert workdir == workspace
    assert stats["passed"] is True
    assert (workdir / "main.py").read_text(encoding="utf-8") == "def answer(): return 42\n"
    assert "main.py" in requests[0]["messages"][1]["content"]
    assert "main.py" in requests[1]["messages"][1]["content"]


def test_write_file_enforces_workspace_size_limit(monkeypatch, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "large.txt").write_text("1234", encoding="utf-8")
    monkeypatch.setattr(agent, "MAX_WORKSPACE_BYTES", 5)

    try:
        agent.write_file(workspace, "next.txt", "12")
    except ValueError as exc:
        assert "workspace size limit" in str(exc)
    else:
        raise AssertionError("Expected workspace size limit to be enforced")


def test_parse_plan_structures_json_and_recovers_plain_text():
    structured = agent.parse_plan(
        '{"objective":"Build a parser","steps":["Tokenize input"],'
        '"files":["parser.py"],"tests":["Reject invalid input"],"risks":[]}'
    )
    assert structured["objective"] == "Build a parser"
    assert structured["steps"] == ["Tokenize input"]
    assert structured["files"] == ["parser.py"]
    assert structured["tests"] == ["Reject invalid input"]

    wrapped = agent.parse_plan(
        'Here is the plan:\n```json\n{"objective":"Wrapped plan","steps":["Add parser"]}\n```'
    )
    assert wrapped["objective"] == "Wrapped plan"

    fallback = agent.parse_plan("1. Add validation\n2. Test invalid input")
    assert fallback["steps"] == ["Add validation", "Test invalid input"]


def test_run_tests_uses_process_status_not_only_output(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "1 passed, 1 failed", "")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    output, passed = agent.run_tests(tmp_path)

    assert passed is False
    assert "status 1" in output


def test_run_tests_rejects_empty_test_suite(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 5, "", "no tests ran")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    output, passed = agent.run_tests(tmp_path)

    assert passed is False
    assert "pytest exit status 5" in output
    assert "No tests were collected" in output


def test_finish_retries_when_independent_verification_fails(monkeypatch):
    calls = []

    def completion_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            message = SimpleNamespace(content='{"objective":"Build it","steps":["Implement"]}')
        else:
            tool_call = SimpleNamespace(
                id=f"finish-{len(calls)}",
                function=SimpleNamespace(name="finish", arguments='{"summary":"Done"}'),
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                model_dump=lambda **options: {"role": "assistant", "tool_calls": []},
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(total_tokens=1),
        )

    fake_client = SimpleNamespace(
        api_key="configured",
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion_create)),
    )
    monkeypatch.setattr(agent, "client", fake_client)
    checks = iter([("1 failed", False), ("1 passed", True)])
    monkeypatch.setattr(agent, "run_tests", lambda workdir: next(checks))
    events = []

    _, stats = agent.run_agent("Build it", on_event=events.append, max_steps=3)

    assert stats["passed"] is True
    assert stats["status"] == "passed"
    assert len(calls) == 3
    assert any(event["type"] == "verification_failed" for event in events)
    assert sum(event["type"] == "finish" for event in events) == 1


def test_run_command_never_invokes_a_shell(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "2 passed", "")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent.run_command(tmp_path, "pytest -q") == "2 passed"
    assert captured["args"] == ["pytest", "-q"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == tmp_path


def test_run_command_reports_timeout_with_byte_output(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 2, output=b"still running")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)

    output = agent.run_command(tmp_path, "pytest -q", timeout=2)

    assert "timed out after 2s" in output
    assert "still running" in output
