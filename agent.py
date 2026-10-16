import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

PLANNER = os.getenv("PLANNER_MODEL", "nvidia/Nemotron-3-Ultra-550b-a55b")
DEFAULT_CODER = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
CODER_MODEL_ALIASES = {
    "nvidia/Nemotron-3-Nano-30B-A3B": DEFAULT_CODER,
}
CODER = os.getenv("CODER_MODEL", DEFAULT_CODER)
CODER = CODER_MODEL_ALIASES.get(CODER, CODER)
MAX_FILE_BYTES = 1_000_000
MAX_WORKSPACE_FILES = 200
MAX_WORKSPACE_BYTES = 20_000_000

client = OpenAI(
    base_url="https://api.tokenfactory.nebius.com/v1/",
    api_key=os.getenv("NEBIUS_API_KEY", ""),
    timeout=120,
    max_retries=1,
)

PLANNER_PROMPT = """You are a senior software architect. Analyze the task and return one JSON object only:
{
  "objective": "one sentence describing the outcome",
  "steps": ["ordered implementation step"],
  "files": ["path/to/file.py — purpose"],
  "tests": ["behavior or edge case to verify"],
  "risks": ["assumption or risk to watch"]
}
Keep each list concise and specific. Use Python + pytest. Do not include markdown fences or code."""

CODER_PROMPT = """You are an autonomous coding agent working in a sandbox directory.
Workflow: write the implementation AND discoverable pytest tests in the workspace, run `python -m pytest -q`, read failures, fix, repeat.
Before calling finish(), confirm pytest collected and passed at least one test. A "no tests ran" result is a failure: create tests for the implementation, then run pytest again.
Avoid path conflicts: never create a file and later use that same path as a directory. If write_file returns an ERROR, adjust the conflicting paths and continue.
Only call finish() when pytest passes. Be concise. Fix the root cause, never weaken tests to pass."""

TOOLS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a file in the workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command (python or pytest only) in the workspace.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call when all tests pass.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]

def safe_path(workdir: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("file path must be a non-empty relative path")
    root = workdir.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ValueError("path escapes workspace")
    return p


def write_file(workdir: Path, rel: str, content: str) -> Path:
    if not isinstance(content, str):
        raise ValueError("file content must be text")
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_FILE_BYTES:
        raise ValueError("file content exceeds the 1 MB per-file limit")
    path = safe_path(workdir, rel)
    root = workdir.resolve()
    parent = path.parent
    while parent != root and not parent.exists():
        parent = parent.parent
    if parent.exists() and not parent.is_dir():
        conflict = parent.relative_to(root).as_posix()
        raise ValueError(
            f"cannot create '{rel}': '{conflict}' already exists as a file; "
            "choose a different path or remove the file/directory name conflict"
        )
    if path.exists() and path.is_dir():
        raise ValueError(
            f"cannot write '{rel}': that path already exists as a directory; "
            "choose a file path that does not conflict"
        )
    existing_files = [item for item in root.rglob("*") if item.is_file()]
    if not path.is_file() and len(existing_files) >= MAX_WORKSPACE_FILES:
        raise ValueError(f"workspace file limit reached ({MAX_WORKSPACE_FILES} files)")
    workspace_bytes = sum(item.stat().st_size for item in existing_files if item != path)
    if workspace_bytes + content_bytes > MAX_WORKSPACE_BYTES:
        raise ValueError("workspace size limit reached (20 MB)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _decode_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_command(workdir: Path, command: str, timeout=20) -> str:
    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"ERROR: invalid command syntax: {exc}"
    if not args or Path(args[0]).name.lower() not in {
        "pytest", "pytest.exe", "python", "python.exe",
    }:
        return "ERROR: only pytest/python commands are allowed."
    if Path(args[0]).name.lower() in {"python", "python.exe"}:
        args[0] = sys.executable
    try:
        r = subprocess.run(args, shell=False, cwd=workdir, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        output = _decode_process_output(exc.stdout) + _decode_process_output(exc.stderr)
        return f"ERROR: command timed out after {timeout}s.\n{output[-3000:]}"
    except OSError as exc:
        return f"ERROR: could not start command: {exc}"
    output = (r.stdout + r.stderr)[-3000:] or "(no output)"
    if r.returncode:
        return f"Command exited with status {r.returncode}.\n{output}"
    return output


def run_tests(workdir: Path, timeout=60) -> tuple[str, bool]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            shell=False,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (_decode_process_output(exc.stdout) + _decode_process_output(exc.stderr))[-3000:]
        return f"Tests timed out after {timeout}s.\n{output}", False
    except OSError as exc:
        return f"Could not start pytest: {exc}", False

    output = (result.stdout + result.stderr)[-3000:] or "(no test output)"
    passed_count = re.search(r"(?m)(\d+) passed(?:\s|$)", output)
    passed = result.returncode == 0 and passed_count is not None and int(passed_count.group(1)) > 0
    if "no tests ran" in output.lower() or "collected 0 items" in output.lower():
        output = f"No tests were collected (pytest exit status {result.returncode}). Add discoverable pytest tests to the workspace and rerun verification.\n{output}"
    elif result.returncode == 0 and not passed:
        output = f"{output}\nNo tests passed; add pytest tests before claiming completion."
    elif result.returncode:
        output = f"Pytest exited with status {result.returncode}.\n{output}"
    return output, passed


def parse_plan(plan_text: str | None) -> dict[str, object]:
    raw = (plan_text or "").strip()
    candidate = raw
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        data = {}
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", candidate):
            try:
                parsed, _ = decoder.raw_decode(candidate[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                data = parsed
                break

    def values(key: str) -> list[str]:
        value = data.get(key, []) if isinstance(data, dict) else []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()][:8]

    objective = data.get("objective", "") if isinstance(data, dict) else ""
    objective = objective.strip() if isinstance(objective, str) else ""
    steps = values("steps")
    if not steps and raw:
        steps = [
            re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
            for line in raw.splitlines()
            if line.strip()
        ][:8]
    if not objective:
        objective = steps[0] if steps else "Implement the requested change and verify it with tests."
    return {
        "objective": objective,
        "steps": steps or ["Implement the requested behavior."],
        "files": values("files"),
        "tests": values("tests"),
        "risks": values("risks"),
        "raw": raw,
    }


def run_agent(
    task: str,
    on_event=print,
    max_steps=12,
    *,
    planner_model=None,
    coder_model=None,
    on_workspace=None,
):
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Describe the coding task before starting.")
    if not isinstance(max_steps, int) or not 1 <= max_steps <= 20:
        raise ValueError("max_steps must be between 1 and 20.")
    if not client.api_key:
        raise RuntimeError("NEBIUS_API_KEY is not configured. Add it to the project .env file and restart the app.")

    workdir = Path(tempfile.mkdtemp(prefix="patchpilot_"))
    if on_workspace:
        on_workspace(workdir)
    stats = {"tokens": 0, "steps": 0, "passed": False, "status": "running", "start": time.time()}

    # --- Phase 1: plan with the big model
    on_event({"type": "phase", "stage": "planning", "message": "The planner is breaking your request into an implementation plan."})
    plan = client.chat.completions.create(
        model=planner_model or PLANNER, temperature=0.3,
        messages=[{"role": "system", "content": PLANNER_PROMPT},
                  {"role": "user", "content": task}])
    plan_text = plan.choices[0].message.content or ""
    stats["tokens"] += plan.usage.total_tokens if plan.usage else 0
    structured_plan = parse_plan(plan_text)
    on_event({"type": "plan", "text": plan_text, "plan": structured_plan})

    # --- Phase 2: code/test/fix loop with the fast model
    on_event({"type": "phase", "stage": "building", "message": "The coding agent is implementing the plan and preparing tests."})
    messages = [{"role": "system", "content": CODER_PROMPT},
                {"role": "user", "content": f"Task: {task}\n\nPlan:\n{plan_text}"}]

    for step in range(max_steps):
        stats["steps"] = step + 1
        resp = client.chat.completions.create(
            model=coder_model or CODER, messages=messages, tools=TOOLS, temperature=0.2)
        stats["tokens"] += resp.usage.total_tokens if resp.usage else 0
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            messages.append({"role": "user", "content": "Continue the implementation. Use write_file and run_command to implement and test your work. Call finish() only after pytest passes."})
            continue

        verification_failed = False
        for call in msg.tool_calls:
            name = call.function.name
            args: dict[str, object] = {}
            try:
                parsed_args = json.loads(call.function.arguments)
                if not isinstance(parsed_args, dict):
                    raise ValueError("tool arguments must be a JSON object")
                args = parsed_args
                if name == "write_file":
                    write_file(workdir, args["path"], args["content"])
                    result = f"wrote {args['path']}"
                    on_event({"type": "write", "path": args["path"], "content": args["content"]})
                elif name == "run_command":
                    result = run_command(workdir, args["command"], timeout=60)
                    on_event({"type": "run", "command": args["command"], "output": result})
                elif name == "finish":
                    on_event({"type": "phase", "stage": "verifying", "message": "Running the full test suite independently before marking this build complete."})
                    check, stats["passed"] = run_tests(workdir)
                    stats["test_output"] = check
                    if stats["passed"]:
                        stats["status"] = "passed"
                        on_event({"type": "finish", "summary": args.get("summary", "Agent requested verification."), "verified": True, "output": check})
                        stats["seconds"] = round(time.time() - stats["start"], 1)
                        return workdir, stats
                    stats["status"] = "running"
                    result = f"Independent verification failed. Fix the tests and implementation, rerun pytest, and only call finish() after tests pass.\n{check}"
                    on_event({"type": "verification_failed", "output": check})
                    on_event({"type": "phase", "stage": "building", "message": "The agent is using the independent test failures to make another correction."})
                    verification_failed = True
                else:
                    result = f"ERROR: unsupported tool '{name}'."
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
                result = f"ERROR: invalid or failed {name} call: {exc}"
                if name == "write_file":
                    on_event({"type": "write_error", "path": args.get("path", ""), "message": str(exc)})
                else:
                    on_event({"type": "tool_error", "tool": name, "message": str(exc)})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if verification_failed:
                break
        if verification_failed:
            messages.append({"role": "user", "content": "The independent test run failed. Read its output, fix the underlying issue, rerun pytest, and continue. Do not call finish() until all tests pass."})

    stats["status"] = "failed"
    stats["failure_reason"] = f"Reached the {max_steps}-iteration limit before the agent verified the tests."
    on_event({"type": "limit_reached", "message": stats["failure_reason"]})
    stats["seconds"] = round(time.time() - stats["start"], 1)
    return workdir, stats