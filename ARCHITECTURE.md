# PatchPilot architecture and operating guide

PatchPilot is a local-first AI coding assistant. A user describes a coding task
in a React interface. A FastAPI service sends the task to a planner and a coding
model, applies the coding model's file and command actions in a per-run temporary
workspace, and streams progress back to the browser. Before a run is called
successful, the backend independently executes the workspace's pytest suite.

This document describes the current implementation, how to run it, the main
request and event flows, and the security boundaries to understand before using
it.

## 1. Component overview

### React frontend

The single-page client is in `frontend/src/`:

- `App.tsx` renders the task composer, optional project ZIP import, model and
  iteration settings, current plan, run progress, project files and text
  preview, change review, download action, and guided activity feed.
- `styles.css` provides the responsive visual system and small-screen layout.
- `main.tsx` mounts the React application.
- `vite.config.ts` serves the UI during development and proxies `/api` calls to
  FastAPI at `http://127.0.0.1:8000`.
- `scripts/dev.mjs` makes `npm run dev` a combined local launcher. It checks
  whether the API health endpoint is already available; if not, it starts
  Uvicorn from the project root and waits for readiness before starting Vite.

The browser receives model identifiers and a boolean indicating whether a
server-side API key is configured. The API key itself is never returned to the
client.

### FastAPI service

`app.py` is the HTTP API and process-level run coordinator. It safely extracts
bounded project ZIPs into temporary snapshots, validates run requests, stores
each active or retained run in an in-memory registry, invokes the agent in a
worker thread, streams run events using Server-Sent Events (SSE), and exposes
workspace files, text previews, unified diffs, and ZIP downloads.

Run records hold a thread-safe event queue, the run's temporary workspace path,
the original project snapshot (when supplied), and completion statistics. The
registry retains at most 30 runs and 10 unique imported project snapshots;
completed runs are evicted before a new run is rejected. Unreferenced imported
snapshots are deleted when removed or when their last retained run is evicted.
Run records and workspaces are not persisted across API process restarts.

### Agent

`agent.py` implements the plan / code / test / repair loop:

1. Ask the planner model to return a JSON plan with an objective, ordered
   implementation steps, files to create, tests to cover, and risks.
2. Normalize the plan into a stable structure. If the model wraps JSON in
   Markdown or returns plain text, attempt recovery and provide a readable
   fallback.
3. For imported projects, make a bounded file index available to both models
   and let the coding model inspect selected UTF-8 files with `read_file`.
4. Ask the coding model to work from the task and plan using the exposed
   `list_files`, `read_file`, `write_file`, `run_command`, and `finish` tools.
5. Apply writes beneath a unique temporary workspace, run allowed commands
   without a shell, and feed tool results (including recoverable errors) back to
   the coding model.
6. When the model requests `finish`, independently invoke pytest. Accept the
   result only if the process exits successfully and reports at least one
   passing test. Otherwise return the failure output to the model and allow it
   to repair the implementation and try again.
7. If the model fails, the provider request fails, or the iteration budget is
   exhausted, return a failed run with an explicit reason instead of a
   success-shaped result.

The OpenAI-compatible client targets Nebius Token Factory. The client uses a
120-second request timeout and one SDK retry for transient request failures.

## 2. Local setup and launch

### Prerequisites

- Python and Node.js/npm installed.
- A Nebius Token Factory API key with access to the configured models.
- A root `.env` file containing the key:

  ```dotenv
  NEBIUS_API_KEY=your-key
  ```

  Do not commit `.env` or paste the key into frontend code.

### Install dependencies

From the project root, install Python dependencies into the project's virtual
environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Install frontend dependencies:

```powershell
cd frontend
npm install
```

### Start the development app

From `frontend/`, run:

```powershell
npm run dev
```

This command starts FastAPI on `127.0.0.1:8000` if it is not already responding
to `/api/health`, then starts the Vite server. Use the Vite URL printed in the
terminal, normally `http://127.0.0.1:5173`. Keep the terminal open while using
the app; stopping the launcher also stops the servers it started. If a Vite
port is occupied, Vite may select and print another port.

To run only the backend manually, use a separate terminal at the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Then start Vite in `frontend/` using `npm run dev:ui`. The development server
proxies `/api` to the backend address above.

### Build and serve the production frontend locally

Build the client from `frontend/`:

```powershell
npm run build
```

The static bundle is written to `frontend/dist/`. When that directory exists,
FastAPI serves the built client as well as the API. Start the backend from the
project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. In this mode, the development launcher and Vite
proxy are not needed.

## 3. Configuration

Configuration is loaded by Python from the environment and the root `.env`
file:

| Variable | Purpose | Default |
| --- | --- | --- |
| `NEBIUS_API_KEY` | Authenticates server-side requests to Nebius Token Factory. | None; runs fail with a setup message if missing. |
| `PLANNER_MODEL` | Model used to produce the implementation plan. | `nvidia/Nemotron-3-Ultra-550b-a55b` |
| `CODER_MODEL` | Model used for implementation and repair. | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |

The legacy coder-model spelling `nvidia/Nemotron-3-Nano-30B-A3B` is normalized
to the supported ID above. Model names can also be adjusted in the run settings
in the UI. Model settings supplied by the UI are request values; credentials
remain exclusively on the server.

## 4. Run lifecycle

1. **Import (optional):** The browser sends a ZIP file to `POST /api/projects`.
   The API enforces compressed, uncompressed, and file-count limits; rejects
   path traversal, symlinks, duplicate paths, encrypted entries, and unsupported
   compression; omits common secret files, Git metadata, and dependency caches;
   then records the extracted snapshot under an opaque project ID.
2. **Submit:** The browser sends a JSON request to `POST /api/runs`, optionally
   including that project ID. The API validates the task, model identifiers,
   and iteration limit, creates a run ID and event queue, then starts the agent
   on a daemon worker thread. It returns the run ID with HTTP 202 without
   waiting for model inference.
3. **Plan:** The agent emits a planning phase event, calls the planner, and
   emits a plan event containing both the original text and normalized
   structure. The UI renders the objective and separately divided steps, files,
   tests, and risks.
4. **Build:** The agent copies the imported snapshot to a new run workspace.
   The coder receives the task, plan, and safe file index. It can list files and
   read bounded UTF-8 source files before changing them. Each tool call is
   executed by the backend. Successful writes appear in the file list; conflicts
   and invalid tool calls are surfaced as recoverable errors so the coder can
   revise its action. Command output is returned to the coder and summarized in
   the activity feed.
5. **Verify and repair:** `finish` does not trust the model's claim. The backend
   launches pytest independently. A failing test run emits a verification
   failure and its output, then returns that result to the coder for another
   correction attempt. An empty test suite does not count as a pass.
6. **Complete or stop:** A passing independent check emits a verified finish
   event. Provider or orchestration exceptions emit an error event and failed
   statistics. If the maximum iteration count is reached without verified
   tests, the run stops as failed and explains that the limit was reached.
7. **Review and export:** The frontend retrieves the final file list and a
   bounded unified diff against the original snapshot. Users can open bounded
   UTF-8 text previews, inspect added/modified/deleted files, and download the
   resulting workspace as a ZIP. The activity guide recommends the next action.

## 5. HTTP API

| Method and route | Purpose |
| --- | --- |
| `GET /api/health` | Returns a lightweight API health status. |
| `GET /api/config` | Returns planner and coder model IDs plus whether an API key is configured. It never returns the key. |
| `POST /api/projects` | Accepts a multipart ZIP upload. Compressed size is limited to 10 MB; extracted project size is limited to 20 MB and 200 files. |
| `DELETE /api/projects/{project_id}` | Removes an imported project; snapshots referenced by retained runs stay available for diff review. |
| `POST /api/runs` | Validates a task and starts a run. The JSON body contains `task`, `planner_model`, `coder_model`, `max_steps`, and optional `project_id`. The step limit is 1–20. |
| `GET /api/runs/{run_id}/events` | Streams newline-delimited JSON SSE messages until completion, error, or client disconnect. Periodic SSE comments keep idle connections alive. |
| `GET /api/runs/{run_id}/files` | Lists up to 100 safe workspace files after or during a run when available. |
| `GET /api/runs/{run_id}/files/{path}` | Returns a UTF-8 text preview for a file in the run workspace. Files over 150 KB are not previewed. |
| `GET /api/runs/{run_id}/diff` | Returns changed paths and unified text diffs, bounded to 1 MB total and 150 KB per file. |
| `GET /api/runs/{run_id}/download` | Returns the run workspace as a ZIP, omitting common secrets and dependency/cache directories. |

Unknown run IDs return 404. File paths are resolved under the run's workspace;
outside paths are rejected. Invalid request bodies are rejected by FastAPI/Pydantic.
Runs and imported-project IDs are held in process memory, so API restarts
invalidate them and remove their temporary workspace references.

## 6. Agent tools and limits

- `write_file(path, content)` accepts relative paths within the run workspace
  and refuses paths reserved for secrets or generated dependencies/caches.
  It detects file-versus-directory collisions, rejects paths that escape the
  workspace, limits a single file to 1 MB, and caps a workspace at 200 files
  and 20 MB.
- `list_files()` returns at most 100 paths and omits common secret files,
  dependency/cache directories, and symlinks.
- `read_file(path)` reads only UTF-8 text up to 100 KB and rejects paths outside
  the workspace, symlinks, and files matching the secret/cache exclusions.
- `run_command(command)` accepts only commands whose executable is `pytest` or
  `python`. It tokenizes arguments and invokes subprocesses with `shell=False`.
  Python commands use the active Python interpreter. Commands have a timeout
  and return a bounded output excerpt.
- `finish(summary)` triggers the separate full pytest verification described
  above. It is not itself proof of success.

The generated workspace is temporary and separate for each run. Imported ZIP
projects are untrusted input; archive checks prevent common extraction attacks
but do not inspect source code for malicious behavior. The temporary directory
is **not** an operating-system security sandbox: model-generated or uploaded
Python code and pytest tests execute as the local user. Command allowlisting
does not make arbitrary Python code safe. Do not expose this service to
untrusted users or run generated code you have not reviewed. For a hosted or
multi-user product, execute each run inside a properly isolated container or
VM with strict CPU, memory, filesystem, process, network, and time limits.

## 7. Event format and UI interpretation

Events are JSON objects with a `type` field. Common events include:

- `phase`: current stage (`planning`, `building`, or `verifying`) and a human
  explanation of what is happening.
- `plan`: planner text plus normalized `objective`, `steps`, `files`, `tests`,
  `risks`, and fallback `raw` content.
- `write` / `write_error`: generated file path and content, or a recoverable
  file-write failure.
- `run`: command and its output.
- `tool_error`: invalid or failed model tool call.
- `verification_failed`: failed independent pytest output that will be sent
  back to the coding model for repair.
- `finish`: final verification summary and a boolean `verified`.
- `limit_reached`: the agent ran out of iterations without verification.
- `error`: an unrecoverable provider or orchestration error.
- `complete`: final run ID and statistics (`passed`, `status`, `steps`,
  `tokens`, and `seconds`).

The UI uses stage and verification events—not raw command text alone—to
calculate progress. The activity guide turns the event history into a concise
status, explains what the result means, and recommends what to do next. Detailed
command output remains available beneath the guidance.

## 8. Validation and troubleshooting

Run the focused Python tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest .\tests -q
```

Build and type-check the frontend from `frontend/`:

```powershell
npm run build
```

If the browser reports a Vite proxy error such as `ECONNREFUSED 127.0.0.1:8000`,
the API is not listening. Stop the old frontend terminal and restart with
`npm run dev` from `frontend/`; the launcher starts the API when it is absent.
If the UI reports a missing API key, check the root `.env` file and restart the
API process so it reloads the environment. If a model call returns an error,
check the API terminal for the provider response and confirm the model ID and
account access in the run settings.
