import json
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
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-test-key")
    monkeypatch.setattr(agent, "search_tavily", lambda query: [{
        "title": "Current docs",
        "url": "https://example.com/docs",
        "content": "Relevant API guidance.",
        "score": 0.9,
    }])
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
        use_world_knowledge=True,
    )

    assert workdir == workspace
    assert stats["passed"] is True
    assert (workdir / "main.py").read_text(encoding="utf-8") == "def answer(): return 42\n"
    assert "main.py" in requests[0]["messages"][1]["content"]
    assert "main.py" in requests[1]["messages"][1]["content"]
    assert "Relevant API guidance." in requests[0]["messages"][1]["content"]
    assert "Relevant API guidance." in requests[1]["messages"][1]["content"]


def test_search_tavily_sends_key_server_side_and_returns_valid_sources(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return json.dumps({
                "results": [
                    {
                        "title": "FastAPI guide",
                        "url": "https://fastapi.tiangolo.com/tutorial/",
                        "raw_content": "Use the documented request pattern.",
                        "score": 0.98,
                    },
                    {
                        "title": "Unsafe link",
                        "url": "javascript:alert(1)",
                        "content": "not a valid source",
                    },
                ],
            }).encode()

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)

    sources = agent.search_tavily("Build a FastAPI application", "private-test-key")

    payload = json.loads(captured["request"].data)
    assert captured["request"].full_url == agent.TAVILY_SEARCH_URL
    assert captured["timeout"] == 30
    assert payload["api_key"] == "private-test-key"
    assert payload["query"] == "Build a FastAPI application"
    assert payload["search_depth"] == "advanced"
    assert len(sources) == 1
    assert sources[0]["title"] == "FastAPI guide"
    assert sources[0]["url"] == "https://fastapi.tiangolo.com/tutorial/"
    assert sources[0]["content"] == "Use the documented request pattern."


def test_search_tavily_fails_clearly_for_missing_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    try:
        agent.search_tavily("Find current framework documentation")
    except RuntimeError as exc:
        assert "TAVILY_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected missing Tavily configuration to be reported")


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
        '{"objective":"Build a parser","architecture":{'
        '"components":[{"id":"api","name":"API","description":"Handle requests"},'
        '{"id":"store","name":"Store","description":"Persist data"}],'
        '"connections":[{"from":"api","to":"store","label":"Read/write"}]},'
        '"steps":["Tokenize input"],'
        '"files":["parser.py"],"tests":["Reject invalid input"],"risks":[]}'
    )
    assert structured["objective"] == "Build a parser"
    assert structured["architecture"] == {
        "components": [
            {"id": "api", "name": "API", "description": "Handle requests"},
            {"id": "store", "name": "Store", "description": "Persist data"},
        ],
        "connections": [{"from": "api", "to": "store", "label": "Read/write"}],
    }
    assert structured["steps"] == ["Tokenize input"]
    assert structured["files"] == ["parser.py"]
    assert structured["tests"] == ["Reject invalid input"]

    wrapped = agent.parse_plan(
        'Here is the plan:\n```json\n{"objective":"Wrapped plan","steps":["Add parser"]}\n```'
    )
    assert wrapped["objective"] == "Wrapped plan"
    assert wrapped["architecture"] == {"components": [], "connections": []}

    fallback = agent.parse_plan("1. Add validation\n2. Test invalid input")
    assert fallback["steps"] == ["Add validation", "Test invalid input"]
    assert fallback["architecture"] == {"components": [], "connections": []}


def test_parse_plan_drops_invalid_architecture_connections():
    structured = agent.parse_plan(
        '{"architecture":{"components":[{"id":"api","name":"API","description":"Requests"},'
        '{"id":"bad id","name":"Invalid","description":"Invalid id"}],'
        '"connections":[{"from":"api","to":"missing","label":"Unknown"},'
        '{"from":"api","to":"api","label":"Self"}]}}'
    )

    assert structured["architecture"] == {
        "components": [{"id": "api", "name": "API", "description": "Requests"}],
        "connections": [],
    }


def test_adaptive_step_budget_scales_with_project_complexity():
    simple_plan = {"steps": ["Implement"], "files": ["app.py"], "tests": ["Verify"]}
    complex_plan = {
        "steps": ["Implement", "Integrate", "Verify", "Harden", "Document"],
        "files": ["api.py", "store.py", "worker.py", "tests.py"],
        "tests": ["Success", "Failure", "Edge cases"],
    }

    simple = agent.adaptive_step_budget("Build a small tool", simple_plan, 30, "")
    complex_project = agent.adaptive_step_budget(
        "Build a larger service",
        complex_plan,
        30,
        "\n".join(f"module_{index}.py" for index in range(20)),
    )

    assert 18 <= simple < complex_project <= 30
    assert agent.adaptive_step_budget("task", complex_plan, 22, "") == 22


def test_compact_conversation_keeps_task_and_recent_tool_turns():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "original task"},
    ]
    for turn in range(8):
        messages.extend([
            {"role": "assistant", "content": f"turn {turn}"},
            {"role": "tool", "content": f"tool result {turn}"},
        ])

    compacted = agent.compact_conversation(messages, "pytest failed: assertion mismatch", keep_turns=3)

    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert "pytest failed" in compacted[2]["content"]
    assert len([message for message in compacted if message.get("role") == "assistant"]) == 3
    assert compacted[-1]["content"] == "tool result 7"


def test_run_agent_automatically_extends_budget_when_progressing(monkeypatch, tmp_path):
    calls = []

    def completion_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            message = SimpleNamespace(content='{"objective":"Build it","steps":["Implement"]}')
        elif len(calls) == 2:
            tool_call = SimpleNamespace(
                id="write-1",
                function=SimpleNamespace(name="write_file", arguments='{"path":"app.py","content":"print(1)"}'),
            )
            message = SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                model_dump=lambda **options: {"role": "assistant", "tool_calls": []},
            )
        else:
            tool_call = SimpleNamespace(
                id="finish-1",
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
    monkeypatch.setattr(agent, "adaptive_step_budget", lambda *args: 1)
    monkeypatch.setattr(agent, "MAX_AGENT_TURNS", 2)
    monkeypatch.setattr(agent, "run_tests", lambda workdir: ("1 passed", True))
    events = []

    workdir, stats = agent.run_agent("Build it", on_event=events.append, max_steps=1)

    assert (workdir / "app.py").read_text(encoding="utf-8") == "print(1)"
    assert stats["passed"] is True
    assert stats["steps"] == 2
    assert stats["max_steps"] == 2
    assert any(event["type"] == "budget_extended" for event in events)


def test_run_agent_stops_after_repeated_nonproductive_turns(monkeypatch):
    calls = []

    def completion_create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(
            content="I will continue.",
            tool_calls=None,
            model_dump=lambda **options: {"role": "assistant", "content": "I will continue."},
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
    monkeypatch.setattr(
        agent,
        "run_tests",
        lambda workdir: ("No tests were collected.", False),
    )
    events = []

    _, stats = agent.run_agent("Build it", on_event=events.append, max_steps=30)

    assert stats["passed"] is False
    assert stats["steps"] == agent.MAX_IDLE_TURNS
    assert "no implementation or test progress" in stats["failure_reason"]
    assert stats["test_output"] == "No tests were collected."
    assert not any(event["type"] == "budget_extended" for event in events)


def test_run_agent_verifies_successful_pytest_command_without_finish(monkeypatch):
    calls = []

    def completion_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            message = SimpleNamespace(content='{"objective":"Build it","steps":["Implement"]}')
        else:
            tool_call = SimpleNamespace(
                id="pytest-1",
                function=SimpleNamespace(name="run_command", arguments='{"command":"python -m pytest -q"}'),
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
    monkeypatch.setattr(agent, "adaptive_step_budget", lambda *args: 1)
    monkeypatch.setattr(agent, "run_command", lambda *args, **kwargs: "2 passed in 0.01s")
    monkeypatch.setattr(agent, "run_tests", lambda workdir: ("2 passed in 0.02s", True))
    events = []

    _, stats = agent.run_agent("Build it", on_event=events.append, max_steps=1)

    assert stats["passed"] is True
    assert stats["status"] == "passed"
    assert stats["test_output"] == "2 passed in 0.02s"
    assert len(calls) == 2
    assert any(event["type"] == "finish" and event["verified"] for event in events)


def test_run_agent_runs_final_verification_after_agent_stops(monkeypatch):
    def completion_create(**kwargs):
        message = SimpleNamespace(
            content="I will continue.",
            tool_calls=None,
            model_dump=lambda **options: {"role": "assistant", "content": "I will continue."},
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
    monkeypatch.setattr(agent, "run_tests", lambda workdir: ("1 passed in 0.01s", True))
    events = []

    _, stats = agent.run_agent("Build it", on_event=events.append, max_steps=30)

    assert stats["passed"] is True
    assert stats["status"] == "passed"
    assert stats["test_output"] == "1 passed in 0.01s"
    assert any(event["type"] == "finish" and event["verified"] for event in events)


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
    assert stats["max_steps"] == 3
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
