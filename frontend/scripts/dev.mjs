import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const frontendDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const projectDirectory = resolve(frontendDirectory, "..");
const apiUrl = "http://127.0.0.1:8000/api/health";
const pythonFromVenv = resolve(
  projectDirectory,
  process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python",
);

let apiProcess;
let uiProcess;
let stopping = false;

async function isApiReady() {
  try {
    const response = await fetch(apiUrl, { signal: AbortSignal.timeout(1000) });
    return response.ok;
  } catch {
    return false;
  }
}

function waitForExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ code: child.exitCode, signal: child.signalCode });
  }
  return new Promise((resolveExit) => {
    child.once("exit", (code, signal) => resolveExit({ code, signal }));
  });
}

async function stopOwnedProcesses() {
  if (stopping) return;
  stopping = true;
  const processes = [uiProcess, apiProcess].filter(Boolean);
  for (const child of processes) {
    if (child.exitCode === null) child.kill();
  }
  await Promise.all(processes.map(waitForExit));
}

process.on("SIGINT", () => void stopOwnedProcesses().then(() => process.exit(0)));
process.on("SIGTERM", () => void stopOwnedProcesses().then(() => process.exit(0)));

async function waitForApi(child) {
  const exited = waitForExit(child);
  const deadline = Date.now() + 30_000;

  while (Date.now() < deadline) {
    const result = await Promise.race([
      isApiReady().then((ready) => ({ ready })),
      exited.then((exit) => ({ exit })),
    ]);
    if ("exit" in result) {
      throw new Error(`FastAPI exited before becoming ready (code ${result.exit.code ?? result.exit.signal}).`);
    }
    if (result.ready) return;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 300));
  }

  throw new Error("FastAPI did not become ready on 127.0.0.1:8000 within 30 seconds.");
}

try {
  if (await isApiReady()) {
    console.log("FastAPI is already running at http://127.0.0.1:8000");
  } else {
    const python = existsSync(pythonFromVenv) ? pythonFromVenv : "python";
    apiProcess = spawn(
      python,
      ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
      { cwd: projectDirectory, stdio: "inherit" },
    );
    apiProcess.once("error", (error) => {
      console.error(`Could not start FastAPI: ${error.message}`);
    });
    await waitForApi(apiProcess);
  }

  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  uiProcess = spawn(npm, ["run", "dev:ui"], {
    cwd: frontendDirectory,
    stdio: "inherit",
    shell: process.platform === "win32",
  });

  const uiExit = await waitForExit(uiProcess);
  await stopOwnedProcesses();
  process.exitCode = uiExit.code ?? 1;
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  await stopOwnedProcesses();
  process.exitCode = 1;
}
