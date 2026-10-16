import { useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, ArrowUp, Check, CheckCheck, CheckCircle2, ChevronDown, Circle,
  Clock3, Code2, Command, Copy, Download, FileCode2, FileDiff, Folder,
  Gauge, GitBranch, Layers3, LoaderCircle, LockKeyhole, Plus, Radio,
  RotateCcw, Search, ShieldCheck, Sparkles, Terminal, Upload, XCircle,
} from "lucide-react";

type Config = {
  planner_model: string;
  coder_model: string;
  api_key_configured: boolean;
};

type AgentEvent = {
  type: string;
  [key: string]: unknown;
};

type StructuredPlan = {
  objective: string;
  steps: string[];
  files: string[];
  tests: string[];
  risks: string[];
  raw?: string;
};

type GeneratedFile = { path: string; size: number };
type Preview = { path: string; content: string };
type ImportedProject = { project_id: string; name: string; file_count: number };
type ProjectChange = { path: string; status: "added" | "modified" | "deleted"; diff: string };
type RunStats = {
  passed: boolean;
  status?: "running" | "passed" | "failed";
  failure_reason?: string;
  test_output?: string;
  steps: number;
  tokens: number;
  seconds: number;
};

const examples = [
  { icon: "◈", title: "Build an LRU cache", task: "Build an LRU cache class with get/put, capacity eviction, and O(1) operations." },
  { icon: "⌁", title: "Parse expressions", task: "Implement a safe arithmetic expression parser supporting +, -, *, /, parentheses, and clear errors for invalid input." },
  { icon: "⌘", title: "Merge intervals", task: "Write a function that merges overlapping intervals, handles empty input, and includes pytest coverage for edge cases." },
];

function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [task, setTask] = useState("");
  const [planner, setPlanner] = useState("");
  const [coder, setCoder] = useState("");
  const [maxSteps, setMaxSteps] = useState(8);
  const [runId, setRunId] = useState("");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [files, setFiles] = useState<GeneratedFile[]>([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [stats, setStats] = useState<RunStats | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<"output" | "review" | "activity">("output");
  const [recentTasks, setRecentTasks] = useState<string[]>([]);
  const [project, setProject] = useState<ImportedProject | null>(null);
  const [projectUploading, setProjectUploading] = useState(false);
  const [projectError, setProjectError] = useState("");
  const [changes, setChanges] = useState<ProjectChange[]>([]);
  const [changesTruncated, setChangesTruncated] = useState(false);
  const [selectedChange, setSelectedChange] = useState("");
  const [fileQuery, setFileQuery] = useState("");
  const [copiedFile, setCopiedFile] = useState("");
  const source = useRef<EventSource | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const taskInput = useRef<HTMLTextAreaElement>(null);
  const projectInput = useRef<HTMLInputElement>(null);
  const fileSearchInput = useRef<HTMLInputElement>(null);
  const plannerDisplayName = formatNvidiaModel(planner || config?.planner_model || "nvidia/Nemotron-3-Ultra-550b-a55b");
  const coderDisplayName = formatNvidiaModel(coder || config?.coder_model || "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B");

  useEffect(() => {
    fetch("/api/config")
      .then((response) => {
        if (!response.ok) throw new Error("Could not connect to PatchPilot API.");
        return response.json() as Promise<Config>;
      })
      .then((data) => {
        setConfig(data);
        setPlanner(data.planner_model);
        setCoder(data.coder_model);
      })
      .catch((reason: Error) => setError(reason.message));
    return () => source.current?.close();
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [events]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        taskInput.current?.focus();
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f" && activeTab === "output" && !taskInput.current) {
        event.preventDefault();
        fileSearchInput.current?.focus();
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [activeTab]);

  function clearSession(nextTask = "") {
    if (running) return;
    source.current?.close();
    setEvents([]);
    setFiles([]);
    setPreview(null);
    setPreviewError("");
    setSelectedFile("");
    setFileQuery("");
    setCopiedFile("");
    setStats(null);
    setChanges([]);
    setChangesTruncated(false);
    setSelectedChange("");
    setRunId("");
    setTask(nextTask);
    setError("");
    setActiveTab("output");
  }

  const plan = useMemo(() => {
    const event = events.find((item) => item.type === "plan");
    if (!event) return null;
    if (typeof event.plan === "object" && event.plan !== null) {
      const value = event.plan as Partial<StructuredPlan>;
      return {
        objective: typeof value.objective === "string" ? value.objective : "Implement the requested change and verify it with tests.",
        steps: Array.isArray(value.steps) ? value.steps.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [],
        files: Array.isArray(value.files) ? value.files.filter((item): item is string => typeof item === "string") : [],
        tests: Array.isArray(value.tests) ? value.tests.filter((item): item is string => typeof item === "string") : [],
        risks: Array.isArray(value.risks) ? value.risks.filter((item): item is string => typeof item === "string") : [],
        raw: typeof value.raw === "string" ? value.raw : undefined,
      };
    }
    const raw = String(event.text || "").trim();
    const steps = raw.split(/\r?\n/).map((line) => line.replace(/^\s*(?:[-*]|\d+[.)])\s*/, "").trim()).filter(Boolean);
    return {
      objective: steps[0] || "Implement the requested change and verify it with tests.",
      steps: steps.length ? steps : ["Implement the requested behavior."],
      files: [],
      tests: [],
      risks: [],
      raw,
    };
  }, [events]);
  const progress = useMemo(() => {
    const phase = [...events].reverse().find((event) => event.type === "phase")?.stage;
    const hasPlan = events.some((event) => event.type === "plan");
    const verified = events.some((event) => event.type === "finish" && event.verified === true)
      || Boolean(stats?.passed);
    return [
      { label: "Plan", complete: hasPlan, current: phase === "planning" },
      { label: "Build", complete: phase === "verifying" || verified, current: phase === "building" },
      { label: "Verify", complete: verified, current: phase === "verifying" },
    ];
  }, [events, stats]);
  const changeCounts = useMemo(() => ({
    added: changes.filter((change) => change.status === "added").length,
    modified: changes.filter((change) => change.status === "modified").length,
    deleted: changes.filter((change) => change.status === "deleted").length,
  }), [changes]);
  const visibleFiles = useMemo(
    () => files.filter((file) => file.path.toLowerCase().includes(fileQuery.trim().toLowerCase())),
    [files, fileQuery],
  );

  async function copyPreview() {
    if (!preview) return;
    try {
      await navigator.clipboard.writeText(preview.content);
      setCopiedFile(preview.path);
    } catch {
      setError("Could not copy this file. Check clipboard permissions and try again.");
    }
  }

  async function startRun(nextTask = task) {
    if (!nextTask.trim() || running || projectUploading) return;
    if (!config) {
      setError("The API configuration is still loading or unavailable. Confirm the backend is running, then reload the page.");
      return;
    }
    source.current?.close();
    setTask(nextTask);
    setEvents([]);
    setFiles([]);
    setPreview(null);
    setPreviewError("");
    setSelectedFile("");
    setFileQuery("");
    setCopiedFile("");
    setStats(null);
    setChanges([]);
    setChangesTruncated(false);
    setSelectedChange("");
    setError("");
    setRunning(true);
    setActiveTab("activity");
    setRecentTasks((current) => [nextTask.trim(), ...current.filter((item) => item !== nextTask.trim())].slice(0, 5));
    try {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task: nextTask.trim(),
          planner_model: planner || config?.planner_model,
          coder_model: coder || config?.coder_model,
          max_steps: maxSteps,
          project_id: project?.project_id,
        }),
        signal: AbortSignal.timeout(30_000),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not start the run.");
      if (typeof result.run_id !== "string" || !result.run_id) {
        throw new Error("The API accepted the request but did not return a valid run ID. Check the backend log.");
      }
      setRunId(result.run_id);
      const connection = new EventSource(`/api/runs/${result.run_id}/events`);
      source.current = connection;
      connection.onmessage = (message) => {
        let event: AgentEvent;
        try {
          event = JSON.parse(message.data) as AgentEvent;
        } catch {
          setError("The server sent an unreadable progress update. Check the backend log and restart the run.");
          setRunning(false);
          connection.close();
          return;
        }
        if (event.type === "complete") {
          setStats(normalizeRunStats(event.stats));
          setRunning(false);
          connection.close();
          void Promise.all([
            fetch(`/api/runs/${result.run_id}/files`),
            fetch(`/api/runs/${result.run_id}/diff`),
          ])
            .then(async ([filesResponse, diffResponse]) => {
              if (!filesResponse.ok) throw new Error("The run completed, but its project-file list could not be loaded.");
              if (!diffResponse.ok) throw new Error("The run completed, but its change summary could not be loaded.");
              const [fileData, diffData] = await Promise.all([
                filesResponse.json() as Promise<{ files: GeneratedFile[] }>,
                diffResponse.json() as Promise<{ changes: ProjectChange[]; truncated: boolean }>,
              ]);
              return { fileData, diffData };
            })
            .then(({ fileData, diffData }) => {
              setFiles(fileData.files);
              setSelectedFile(fileData.files[0]?.path || "");
              setChanges(diffData.changes);
              setChangesTruncated(diffData.truncated);
              setSelectedChange(diffData.changes[0]?.path || "");
            })
            .catch((reason: Error) => setPreviewError(reason.message));
        } else if (event.type === "error") {
          setError(String(event.message || "The agent run failed."));
          if (event.stats) setStats(normalizeRunStats(event.stats));
          setRunning(false);
          connection.close();
        } else if (event.type === "write" && typeof event.path === "string") {
          const file = {
            path: event.path,
            size: new TextEncoder().encode(String(event.content || "")).length,
          };
          setFiles((current) => [file, ...current.filter((item) => item.path !== file.path)]);
          setSelectedFile(file.path);
        }
        setEvents((current) => [...current, event]);
      };
      connection.onerror = () => {
        if (connection.readyState === EventSource.CLOSED) {
          setError("The live connection ended before the run completed. Check that the API is still running, then start a new run.");
          setRunning(false);
        }
      };
    } catch (reason) {
      const message = reason instanceof Error && reason.name === "TimeoutError"
        ? "The API did not respond in time. Check that the backend is running, then try again."
        : reason instanceof Error ? reason.message : "Could not start the run.";
      setError(message);
      setRunning(false);
    }
  }

  async function uploadProject(file: File | undefined) {
    if (!file) return;
    setProjectError("");
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setProjectError("Choose a .zip archive of your project.");
      return;
    }
    if (file.size > 10_000_000) {
      setProjectError("Project ZIP files are limited to 10 MB.");
      return;
    }
    setProjectUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/projects", { method: "POST", body: form });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not import this project.");
      setProject(result as ImportedProject);
    } catch (reason) {
      setProjectError(reason instanceof Error ? reason.message : "Could not import this project.");
    } finally {
      setProjectUploading(false);
      if (projectInput.current) projectInput.current.value = "";
    }
  }

  async function removeProject() {
    if (!project) return;
    setProjectError("");
    try {
      const response = await fetch(`/api/projects/${project.project_id}`, { method: "DELETE" });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        throw new Error(typeof result.detail === "string" ? result.detail : "Could not remove the imported project.");
      }
      setProject(null);
    } catch (reason) {
      setProjectError(reason instanceof Error ? reason.message : "Could not remove the imported project.");
    }
  }

  useEffect(() => {
    if (!runId || !selectedFile) {
      setPreview(null);
      setPreviewError("");
      return;
    }
    const controller = new AbortController();
    setPreview(null);
    setPreviewError("");
    fetch(`/api/runs/${runId}/files/${selectedFile.split("/").map(encodeURIComponent).join("/")}`, {
      signal: controller.signal,
    })
      .then(async (response) => {
        const data = await response.json();
        if (response.status === 409 && running) return null;
        if (!response.ok) throw new Error(data.detail || "Could not open this file.");
        return data as Preview;
      })
      .then((data) => { if (data) setPreview(data); })
      .catch((reason: Error) => {
        if (reason.name !== "AbortError") setPreviewError(reason.message);
      });
    return () => controller.abort();
  }, [runId, selectedFile, running]);

  const activityGuide = useMemo(
    () => getActivityGuide(events, running, stats, error),
    [events, running, stats, error],
  );

  const hasRun = events.length > 0 || running || Boolean(stats);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#" aria-label="PatchPilot home">
          <span className="brand-mark"><GitBranch size={19} strokeWidth={2.5} /></span>
          <span>patchpilot<span className="brand-period">.</span></span>
        </a>
        <div className="workspace-label">WORKSPACE</div>
        <div className="workspace-switch">
          <span className="workspace-icon"><Folder size={15} /></span>
          <span>{project?.name || "Local workspace"}</span>
        </div>
        <div className="side-section">
          <div className="side-heading">YOUR WORK</div>
          <button className="side-link active" onClick={() => clearSession()} disabled={running}><Plus size={16} /> New build <span className="shortcut">Ctrl K</span></button>
          {recentTasks.length > 0 && <>
            <div className="side-heading recent-heading">RECENT PROMPTS</div>
            {recentTasks.slice(0, 3).map((prompt, index) => (
              <button className="side-link recent-link" key={`${prompt}-${index}`} title={prompt} onClick={() => clearSession(prompt)} disabled={running}>
                <Clock3 size={14} /><span>{prompt}</span>
              </button>
            ))}
          </>}
        </div>
        <div className="side-bottom">
          <div className="model-card">
              <div className="model-card-head"><span className={`online-dot ${config?.api_key_configured ? "" : "setup-dot"}`} /> AGENT STATUS <span className={`live-label ${config?.api_key_configured ? "" : "setup-label"}`}>{config?.api_key_configured ? "READY" : "SETUP NEEDED"}</span></div>
            <div className="model-name">{coderDisplayName}</div>
            <div className="model-provider">Powered by Nebius Token Factory</div>
          </div>
          <div className="profile">
            <div className="avatar">P</div>
            <div className="profile-copy"><strong>PatchPilot</strong><span>Hackathon build</span></div>
            <button className="icon-button" title="Clear session" onClick={() => clearSession()} disabled={running}><RotateCcw size={15} /></button>
          </div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><span className="crumb-sep">/</span><strong>{hasRun ? "Build session" : "New build"}</strong></div>
          <div className="topbar-right">
            <span className={`connection ${config?.api_key_configured ? "connected" : "disconnected"}`}>
              <span className="connection-dot" />{config?.api_key_configured ? "API connected" : "API key needed"}
            </span>
            <span className="topbar-divider" />
            <button className="icon-button top-icon" title="Focus task prompt (Ctrl+K)" onClick={() => taskInput.current?.focus()}><Command size={16} /></button>
            <div className="avatar small">P</div>
          </div>
        </header>

        <div className={`content ${hasRun ? "content-running" : ""}`}>
          {!hasRun ? (
            <section className="welcome">
              <div className="welcome-intro">
                <div className="eyebrow"><span className="eyebrow-icon"><Sparkles size={13} /></span> YOUR AI ENGINEERING TEAM</div>
                <h1>From idea<br />to <span>verified code.</span></h1>
                <p className="hero-subtitle">A clear brief in. Tested, reviewable code out. Let PatchPilot plan the work, build it, and prove it with tests.</p>
                <div className="model-attribution"><Sparkles size={14} /><strong>Runs on Nebius Token Factory using {plannerDisplayName} (planner) + {coderDisplayName} (coder)</strong></div>
                <div className="welcome-flow" aria-label="How PatchPilot works">
                  <div className="welcome-flow-step"><span>01</span><div><strong>Describe</strong><small>Tell us what you need</small></div></div>
                  <span className="welcome-flow-line" />
                  <div className="welcome-flow-step"><span>02</span><div><strong>Build</strong><small>Watch the agent work</small></div></div>
                  <span className="welcome-flow-line" />
                  <div className="welcome-flow-step"><span>03</span><div><strong>Verify</strong><small>Review tested changes</small></div></div>
                </div>
              </div>

              <div className="welcome-builder">
                <div className="builder-heading">
                  <div><span className="builder-kicker">START A NEW BUILD</span><h2>What would you like to make?</h2></div>
                  <span className="builder-language"><Code2 size={13} /> Python + pytest</span>
                </div>
                <div className="prompt-card">
                  <label className="sr-only" htmlFor="task-input">Describe what you want to build</label>
                  <textarea
                    id="task-input"
                    ref={taskInput}
                    value={task}
                    onChange={(event) => setTask(event.target.value)}
                    onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") void startRun(); }}
                    placeholder="Describe a feature, fix, or tool…&#10;&#10;What should it do? Include any requirements or edge cases."
                    maxLength={4000}
                  />
                  <div className="prompt-footer">
                    <div className="prompt-hint"><span>{task.length}/4000</span><span className="hint-separator">·</span><span>Ctrl + Enter to run</span></div>
                    <button className="submit-button" disabled={!task.trim() || running || projectUploading || !config} onClick={() => void startRun()}>
                      Build with AI <ArrowUp size={16} />
                    </button>
                  </div>
                </div>
                <div className={`project-import ${project ? "has-project" : ""}`}>
                  <input
                    ref={projectInput}
                    className="sr-only"
                    type="file"
                    accept=".zip,application/zip"
                    aria-label="Import a project ZIP"
                    disabled={projectUploading || running}
                    onChange={(event) => void uploadProject(event.target.files?.[0])}
                  />
                  <div className="project-import-copy">
                    <span className="project-import-icon"><Folder size={17} /></span>
                    <span><strong>{project ? project.name : "Already have a project?"}</strong><small>{project ? `${project.file_count} files imported · copied into a run workspace` : "Import a Python project ZIP and ask PatchPilot to improve it."}</small></span>
                  </div>
                  {project ? (
                    <button className="project-clear" type="button" onClick={() => void removeProject()} disabled={projectUploading || running} aria-label="Remove imported project">Remove</button>
                  ) : (
                    <button className="project-upload-button" type="button" onClick={() => projectInput.current?.click()} disabled={projectUploading || running}>
                      {projectUploading ? <LoaderCircle size={14} className="spin" /> : <Upload size={14} />}
                      {projectUploading ? "Importing…" : "Import ZIP"}
                    </button>
                  )}
                </div>
                <div className="project-safety-note"><LockKeyhole size={12} /><span>Secrets and dependency caches are excluded. Only upload code you trust: project tests run on this server.</span></div>
                {projectError && <div className="error-banner project-error"><XCircle size={15} />{projectError}</div>}
                {error && <div className="error-banner"><XCircle size={16} /> {error}</div>}

                <div className="suggestion-heading"><span>OR TRY A QUICK START</span></div>
                <div className="suggestions">
                  {examples.map((example) => (
                    <button className="suggestion-card" key={example.title} disabled={!config || running || projectUploading} onClick={() => { setTask(example.task); void startRun(example.task); }}>
                      <span className="suggestion-icon">{example.icon}</span><span>{example.title}</span><ArrowUp size={14} className="suggestion-arrow" />
                    </button>
                  ))}
                </div>
                <div className="builder-footnote"><ShieldCheck size={13} /><span>Isolated workspace</span><span>·</span><span>Independent test verification</span><span>·</span><span>Review before you ship</span></div>
              </div>
            </section>
          ) : (
            <section className="session">
              <div className="session-heading">
                <div>
                  <div className="eyebrow"><span className="eyebrow-icon"><Sparkles size={13} /></span> BUILD WORKSPACE <span className="session-context">{project ? project.name : "NEW PROJECT"}</span></div>
                  <h1>{running ? "Your build is underway" : stats?.passed ? "Your build is verified" : "Review your build"}</h1>
                  <p className="session-task">{task}</p>
                </div>
                <button className="new-build-button" onClick={() => clearSession()} disabled={running}> <Plus size={15} /> New build</button>
              </div>
              <div className="progress-strip">
                {progress.map((step, index) => (
                  <div className={`progress-step ${step.complete ? "complete" : step.current && running ? "current" : ""}`} key={step.label}>
                    <span className="step-icon">{step.complete ? <Check size={13} /> : step.current && running ? <LoaderCircle size={13} className="spin" /> : <Circle size={13} />}</span>
                    <span>{step.label}</span>{index < progress.length - 1 && <span className="step-connector" />}
                  </div>
                ))}
                {running && <span className="working-label"><span className="pulse-dot" /> Agent working</span>}
                {stats && <span className={`result-label ${stats.passed ? "passed" : "failed"}`}>{stats.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{stats.passed ? "All tests passed" : "Needs attention"}</span>}
              </div>

              {stats && <section className={`build-summary ${stats.passed ? "verified" : "attention"}`} aria-label="Build result summary">
                <div className="build-summary-lead">
                  <span className="summary-status-icon">{stats.passed ? <ShieldCheck size={20} /> : <XCircle size={20} />}</span>
                  <span><strong>{stats.passed ? "Ready for your review" : "This build needs a closer look"}</strong><small>{stats.passed ? "The generated project passed PatchPilot's independent test check." : "The run finished without verified test success. Check Activity for details."}</small></span>
                </div>
                <div className="summary-metrics">
                  <div className="summary-metric"><strong>{changes.length}</strong><span>files changed</span></div>
                  <div className="summary-metric"><strong>{stats.test_output?.match(/(\d+) passed/)?.[1] || "—"}</strong><span>tests passed</span></div>
                  <div className="summary-metric"><strong>{stats.seconds}<small>s</small></strong><span>time elapsed</span></div>
                  <div className="summary-metric"><strong>{stats.steps}<small>/{maxSteps}</small></strong><span>agent steps</span></div>
                </div>
              </section>}

              <div className="results-layout">
                <section className="result-panel">
                  {plan && <PlanPanel plan={plan} />}
                  <div className="panel-tabs">
                    <button className={activeTab === "output" ? "selected" : ""} onClick={() => setActiveTab("output")}><Code2 size={15} /> Files <span className="tab-count">{files.length}</span></button>
                    <button className={activeTab === "review" ? "selected" : ""} onClick={() => setActiveTab("review")}><FileDiff size={15} /> Review <span className="tab-count">{changes.length}</span></button>
                    <button className={activeTab === "activity" ? "selected" : ""} onClick={() => setActiveTab("activity")}><Activity size={15} /> Activity <span className="tab-count">{events.length}</span></button>
                    <div className="panel-spacer" />
                    {stats && files.length > 0 && <a className="download-button" href={`/api/runs/${runId}/download`} download><Download size={13} /> Download ZIP</a>}
                    {stats && <div className="stats-inline"><span><Gauge size={14} /> {stats.steps} steps</span><span><Layers3 size={14} /> {stats.tokens.toLocaleString()} tokens</span><span><Clock3 size={14} /> {stats.seconds}s</span></div>}
                  </div>
                  {activeTab === "output" ? (
                    <div className="file-workspace">
                      <div className="file-tree">
                        <div className="tree-heading"><Folder size={14} /><span>{project ? "PROJECT FILES" : "GENERATED FILES"}</span><span className="file-total">{files.length}</span></div>
                        {files.length > 0 && <label className="file-search"><Search size={13} /><input ref={fileSearchInput} aria-label="Filter project files" placeholder="Filter files…" value={fileQuery} onChange={(event) => setFileQuery(event.target.value)} /><kbd>Ctrl F</kbd></label>}
                        {files.length === 0 ? <div className="empty-files">{running ? "Files will appear as the agent builds…" : "No files were generated yet. Check the Activity tab for the run result."}</div> : visibleFiles.length === 0 ? <div className="empty-files">No files match “{fileQuery}”.</div> : visibleFiles.map((file) => (
                          <button className={`file-row ${selectedFile === file.path ? "selected" : ""}`} key={file.path} onClick={() => setSelectedFile(file.path)}>
                            <FileCode2 size={14} /><span>{file.path}</span><small>{formatFileSize(file.size)}</small>
                          </button>
                        ))}
                      </div>
                      <div className="code-preview">
                        {preview ? <>
                          <div className="code-title">
                            <span><FileCode2 size={14} /> {preview.path}</span>
                            <span className="code-title-actions">
                              <span className="language-label">{getFileLanguage(preview.path)}</span>
                              <button className="copy-button" onClick={() => void copyPreview()} title="Copy file contents">
                                {copiedFile === preview.path ? <CheckCheck size={13} /> : <Copy size={13} />}
                                {copiedFile === preview.path ? "Copied" : "Copy"}
                              </button>
                            </span>
                          </div>
                          <pre className="source-code"><code>{getPreviewLines(preview.content).map((line, index) => <span className="source-line" key={index}><span className="line-number" aria-hidden="true">{index + 1}</span><span className="line-content">{line || " "}</span></span>)}</code></pre>
                        </> : <div className="preview-empty"><Code2 size={22} /><span>{previewError || (running ? "The agent is writing code…" : "Select a generated file to preview it.")}</span></div>}
                      </div>
                    </div>
                  ) : activeTab === "review" ? (
                    <div className="review-workspace">
                      <div className="review-file-list">
                        <div className="tree-heading"><FileDiff size={14} /><span>CHANGED FILES</span><span className="file-total">{changes.length}</span></div>
                        {changes.length > 0 && <div className="change-counts"><span className="added">{changeCounts.added} added</span><span className="modified">{changeCounts.modified} changed</span><span className="deleted">{changeCounts.deleted} removed</span></div>}
                        {changes.length === 0 ? <div className="empty-files">{running ? "Changes will appear when verification finishes…" : "No file changes to review."}</div> : changes.map((change) => (
                          <button className={`review-file-row ${selectedChange === change.path ? "selected" : ""}`} key={change.path} onClick={() => setSelectedChange(change.path)}>
                            <span className={`change-status ${change.status}`}>{change.status === "added" ? "+" : change.status === "deleted" ? "−" : "~"}</span><span>{change.path}</span>
                          </button>
                        ))}
                        {changesTruncated && <div className="diff-truncated">Change preview capped for safety. Download the full workspace to inspect everything.</div>}
                      </div>
                      <div className="diff-preview">
                        {(() => {
                          const change = changes.find((item) => item.path === selectedChange);
                          return change ? <>
                            <div className="code-title"><span><FileDiff size={14} /> {change.path}</span><span className={`change-label ${change.status}`}>{change.status.toUpperCase()}</span></div>
                            <pre className="diff-code">{change.diff.split("\n").map((line, index) => <span className={line.startsWith("+") && !line.startsWith("+++") ? "diff-add" : line.startsWith("-") && !line.startsWith("---") ? "diff-remove" : ""} key={`${index}-${line}`}>{line || " "}</span>)}</pre>
                          </> : <div className="preview-empty"><FileDiff size={22} /><span>{running ? "The agent is working; the review appears after it finishes." : "No changes to review for this run."}</span></div>;
                        })()}
                      </div>
                    </div>
                  ) : (
                    <div className="activity-feed">
                      <ActivityGuide guide={activityGuide} running={running} />
                      {events.filter((event) => event.type !== "complete").map((event, index) => <ActivityItem event={event} key={`${event.type}-${index}`} />)}
                      {running && <div className="activity-thinking"><LoaderCircle size={15} className="spin" /> The agent is working on the next step. You can follow each action below.</div>}
                      {error && <div className="error-banner"><XCircle size={16} />{error}</div>}
                      <div ref={bottomRef} />
                    </div>
                  )}
                </section>
                <aside className="run-sidebar">
                  <div className="run-card">
                    <div className="run-card-title"><Radio size={15} /> RUN OVERVIEW</div>
                    <div className="overview-item"><span>Status</span><strong className={running ? "status-live" : stats?.passed ? "status-ok" : "status-idle"}><span className="status-dot" />{running ? "In progress" : stats?.passed ? "Verified" : stats ? "Review needed" : "Starting"}</strong></div>
                    <div className="overview-item"><span>Planner</span><strong title={planner}>{plannerDisplayName}</strong></div>
                    <div className="overview-item"><span>Coder</span><strong title={coder}>{coderDisplayName}</strong></div>
                    <div className="overview-item"><span>Max iterations</span><strong>{maxSteps}</strong></div>
                    {stats && <div className="overview-item"><span>Tests</span><strong className={stats.passed ? "status-ok" : "status-idle"}>{stats.passed ? "Passing" : "Not verified"}</strong></div>}
                  </div>
                  <details className="settings-card">
                    <summary><span><Gauge size={15} /> RUN SETTINGS</span><ChevronDown size={14} /></summary>
                    <label>Planner model<input value={planner} onChange={(event) => setPlanner(event.target.value)} disabled={running} /></label>
                    <label>Coder model<input value={coder} onChange={(event) => setCoder(event.target.value)} disabled={running} /></label>
                    <label>Max iterations<div className="range-setting"><input type="range" min={1} max={20} value={maxSteps} onChange={(event) => setMaxSteps(Number(event.target.value))} disabled={running} /><span>{maxSteps}</span></div></label>
                  </details>
                  {error && <div className="error-banner compact"><XCircle size={15} /> {error}</div>}
                  {stats && !running && <button className="retry-button" onClick={() => void startRun()}><RotateCcw size={14} /> Run again</button>}
                </aside>
              </div>
            </section>
          )}
        </div>
        <footer className="footer"><span>PATCHPILOT <span className="footer-sep">/</span> YOUR AI BUILD PARTNER</span><span><span className={`footer-dot ${config ? "" : "offline"}`} /> {config ? config.api_key_configured ? "API CONNECTED" : "API KEY REQUIRED" : "API OFFLINE"}</span></footer>
      </main>
    </div>
  );
}

function ActivityItem({ event }: { event: AgentEvent }) {
  if (event.type === "phase") {
    const stage = String(event.stage || "working");
    const label = stage === "planning" ? "Planning the work" : stage === "building" ? "Building the solution" : "Checking the result";
    return <div className="activity-item"><span className="activity-icon plan-icon"><Sparkles size={15} /></span><div className="activity-body"><div className="activity-title">{label}</div><div className="activity-note">{String(event.message || "")}</div></div></div>;
  }
  if (event.type === "plan") return (
    <div className="activity-item plan-activity"><span className="activity-icon plan-icon"><CheckCircle2 size={15} /></span><div className="activity-body"><div className="activity-title">Plan ready</div><div className="activity-note">Review the structured plan above. Next, the coding agent will create the listed files and tests.</div></div></div>
  );
  if (event.type === "write") return (
    <div className="activity-item"><span className="activity-icon write-icon"><FileCode2 size={15} /></span><div className="activity-body"><div className="activity-title">Updated <code>{String(event.path || "file")}</code></div><div className="activity-note">This file is available in the Files tab. The agent will continue implementing, then run the test suite.</div></div></div>
  );
  if (event.type === "write_error") return (
    <div className="activity-item"><span className="activity-icon error-icon"><XCircle size={15} /></span><div className="activity-body"><div className="activity-title">Path needs adjustment: <code>{String(event.path || "file")}</code></div><div className="activity-note">{String(event.message || "The agent was told to choose a non-conflicting file path and continue.")}</div></div></div>
  );
  if (event.type === "verification_failed") return (
    <div className="activity-item"><span className="activity-icon error-icon"><XCircle size={15} /></span><div className="activity-body"><div className="activity-title">Independent check found a failure</div><div className="activity-note">The agent is reviewing this output and will try to fix the issue before reporting a result.</div><pre className="terminal-output">{String(event.output || "No pytest output was produced.")}</pre></div></div>
  );
  if (event.type === "tool_error") return (
    <div className="activity-item"><span className="activity-icon error-icon"><XCircle size={15} /></span><div className="activity-body"><div className="activity-title">Could not complete <code>{String(event.tool || "tool")}</code></div><div className="activity-note">{String(event.message || "The tool call was invalid.")} The agent has received this error and can try a corrected action.</div></div></div>
  );
  if (event.type === "run") {
    const output = String(event.output || "");
    const isPytest = /pytest/.test(String(event.command || ""));
    const succeeded = !output.startsWith("ERROR:") && !output.includes("Command exited with status");
    const passed = isPytest && succeeded && /\d+ passed/.test(output);
    const label = isPytest ? passed ? "Tests passed" : succeeded ? "Test command completed" : "Tests reported an error" : "Command completed";
    const next = isPytest
      ? passed ? "The agent will now review the result and independently verify the full test suite." : "The agent will inspect this output, fix any failures, and run the tests again."
      : "The agent will use this result to decide what to implement next.";
    return <div className="activity-item"><span className={`activity-icon ${passed ? "test-icon" : succeeded ? "terminal-icon" : "error-icon"}`}>{passed ? <CheckCircle2 size={15} /> : succeeded ? <Terminal size={15} /> : <XCircle size={15} />}</span><div className="activity-body"><div className="activity-title">{label}: <code>{String(event.command || "command")}</code></div><div className="activity-note">{next}</div><pre className="terminal-output">{output || "(no output)"}</pre></div></div>;
  }
  if (event.type === "finish") return <div className="activity-item"><span className={`activity-icon ${event.verified ? "test-icon" : "error-icon"}`}>{event.verified ? <CheckCircle2 size={15} /> : <XCircle size={15} />}</span><div className="activity-body"><div className="activity-title">{event.verified ? "Final test verification passed" : "Final test verification failed"}</div><div className="activity-note">{event.verified ? "The build is complete. Review the generated files before using them." : "The build is not marked as successful. Review the test output below, then run the build again after addressing the issue."}</div>{event.output != null && <pre className="terminal-output">{String(event.output)}</pre>}</div></div>;
  if (event.type === "limit_reached") return <div className="activity-item"><span className="activity-icon error-icon"><XCircle size={15} /></span><div className="activity-body"><div className="activity-title">Iteration limit reached</div><div className="activity-note">{String(event.message || "The agent stopped before verifying the tests.")} Increase the iteration limit or narrow the task, then run it again.</div></div></div>;
  if (event.type === "error") return <div className="activity-item"><span className="activity-icon error-icon"><XCircle size={15} /></span><div className="activity-body"><div className="activity-title">The run stopped unexpectedly</div><div className="activity-note">{String(event.message || "")} Check your API key and model settings. Correct the issue, then select “Run again”.</div></div></div>;
  return null;
}

function PlanPanel({ plan }: { plan: StructuredPlan }) {
  const sections = [
    { title: "Implementation steps", items: plan.steps, numbered: true },
    { title: "Files to create", items: plan.files, numbered: false },
    { title: "Test coverage", items: plan.tests, numbered: false },
    { title: "Assumptions & risks", items: plan.risks, numbered: false },
  ].filter((section) => section.items.length > 0);

  return (
    <section className="structured-plan" aria-label="Implementation plan">
      <div className="plan-heading"><span className="activity-icon plan-icon"><Sparkles size={15} /></span><div><div className="plan-kicker">IMPLEMENTATION PLAN</div><h2>{plan.objective}</h2></div></div>
      <div className="plan-sections">
        {sections.map((section) => (
          <div className="plan-section" key={section.title}>
            <h3>{section.title}</h3>
            <ul className={section.numbered ? "numbered-list" : ""}>
              {section.items.map((item, index) => <li key={`${section.title}-${index}`}>{item}</li>)}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

function getActivityGuide(events: AgentEvent[], running: boolean, stats: RunStats | null, error: string) {
  const lastError = [...events].reverse().find((event) => event.type === "error");
  const limitReached = events.some((event) => event.type === "limit_reached");
  const finish = [...events].reverse().find((event) => event.type === "finish");
  const latestPhase = [...events].reverse().find((event) => event.type === "phase");
  const lastPytest = [...events].reverse().find((event) => event.type === "run" && /pytest/.test(String(event.command || "")));
  const testOutput = String(
    finish?.output
      || stats?.test_output
      || [...events].reverse().find((event) => event.type === "verification_failed")?.output
      || lastPytest?.output
      || "",
  );
  const noTestsCollected = /no tests ran|collected 0 items/i.test(testOutput);
  const writes = events.filter((event) => event.type === "write").length;
  const tests = events.filter((event) => event.type === "run" && /pytest/.test(String(event.command || ""))).length;

  if (lastError || error || stats?.status === "failed" && stats.failure_reason) {
    return { title: "This run needs your attention", message: String(lastError?.message || error || stats?.failure_reason), next: "Check the error details in the activity log. Fix the configuration or reduce the task scope, then run it again.", kind: "error" };
  }
  if (limitReached) return {
    title: noTestsCollected ? "No tests were collected before the iteration limit" : "The agent reached its iteration limit",
    message: noTestsCollected
      ? "Pytest did not find discoverable tests, and the agent stopped before it could add them and verify the build."
      : "The requested work may be partially complete, but it was not verified.",
    next: noTestsCollected
      ? "Ask the agent to add test_*.py files and run python -m pytest -q, or increase the iteration limit and rerun."
      : "Review the latest test output, increase the iteration limit or narrow the task, and rerun.",
    kind: "error",
    output: testOutput,
  };
  if (!running && (finish && finish.verified === false || stats && !stats.passed)) {
    return {
      title: noTestsCollected ? "Pytest did not find any tests" : "The tests did not confirm this build",
      message: noTestsCollected
        ? "The generated workspace has no discoverable pytest tests, so PatchPilot cannot verify the result."
        : "The independent pytest check did not pass. PatchPilot will not label an unverified result as successful.",
      next: noTestsCollected
        ? "Add tests in a discoverable test_*.py file, or ask the coding agent to create them, then run the task again."
        : "Read the pytest output below, inspect the generated files, fix the reported issue, and run the task again.",
      kind: "error",
      output: testOutput,
    };
  }
  if (stats?.passed) return { title: "Build verified", message: "The full test suite passed during independent verification.", next: "Open the Files tab to review or copy the generated code before using it.", kind: "success" };
  if (!running) return { title: "Ready to review", message: "The run has ended without a verified success.", next: "Review the latest event and generated files. You can run the task again after addressing any issue.", kind: "neutral" };
  if (latestPhase?.stage === "planning" || events.length === 0) return { title: "Step 1 · Understand and plan", message: "The planner is translating your request into concrete implementation and test steps.", next: "You can review the structured plan above as soon as it is ready.", kind: "active" };
  if (latestPhase?.stage === "verifying") return { title: "Step 3 · Verify the result", message: `The agent has made ${writes} file update${writes === 1 ? "" : "s"} and run ${tests} test command${tests === 1 ? "" : "s"}.`, next: "PatchPilot is running the complete pytest suite independently. A build only passes when this check succeeds.", kind: "active" };
  if (events.some((event) => event.type === "verification_failed")) return { title: "Step 2 · Fix the final test failures", message: "The independent verification caught an issue, so this build is not marked as successful.", next: "PatchPilot is reading the failure details above and will attempt a correction before verifying again.", kind: "active" };
  if (tests > 0) return { title: "Step 2 · Improve from test feedback", message: `The agent has run tests ${tests} time${tests === 1 ? "" : "s"} and changed ${writes} file${writes === 1 ? "" : "s"}.`, next: "It is using the test output to fix problems before the final verification.", kind: "active" };
  return { title: "Step 2 · Implement the plan", message: `${writes} file update${writes === 1 ? "" : "s"} so far. The coding agent is building the solution and its tests.`, next: "Next, the agent will run pytest, inspect failures, and make corrections if needed.", kind: "active" };
}

function normalizeRunStats(value: unknown): RunStats {
  const stats = typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
  const numberOrZero = (item: unknown) => typeof item === "number" && Number.isFinite(item) ? item : 0;
  return {
    passed: stats.passed === true,
    status: stats.status === "passed" || stats.status === "failed" || stats.status === "running"
      ? stats.status
      : stats.passed === true ? "passed" : "failed",
    failure_reason: typeof stats.failure_reason === "string" ? stats.failure_reason : undefined,
    test_output: typeof stats.test_output === "string" ? stats.test_output : undefined,
    steps: numberOrZero(stats.steps),
    tokens: numberOrZero(stats.tokens),
    seconds: numberOrZero(stats.seconds),
  };
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function getFileLanguage(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() || "";
  const languages: Record<string, string> = {
    py: "PYTHON",
    ts: "TYPESCRIPT",
    tsx: "TSX",
    js: "JAVASCRIPT",
    jsx: "JSX",
    json: "JSON",
    md: "MARKDOWN",
    css: "CSS",
    html: "HTML",
    yml: "YAML",
    yaml: "YAML",
    toml: "TOML",
    txt: "TEXT",
  };
  return languages[extension] || (extension ? extension.toUpperCase() : "TEXT");
}

function getPreviewLines(content: string): string[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

function ActivityGuide({ guide, running }: { guide: ReturnType<typeof getActivityGuide>; running: boolean }) {
  const output = "output" in guide && typeof guide.output === "string" ? guide.output : "";
  return (
    <div className={`activity-guide ${guide.kind}`}>
      <div className="guide-title">{running && <LoaderCircle size={14} className="spin" />}{!running && guide.kind === "success" && <CheckCircle2 size={14} />}{!running && guide.kind === "error" && <XCircle size={14} />}{guide.title}</div>
      <p>{guide.message}</p>
      <div className="guide-next"><strong>{guide.kind === "success" || guide.kind === "error" ? "Recommended next step" : "What happens next"}</strong><span>{guide.next}</span></div>
      {output && <details className="guide-output"><summary>Pytest output</summary><pre className="terminal-output">{output}</pre></details>}
    </div>
  );
}

export default App;

function formatNvidiaModel(model: string): string {
  const name = model.split("/").pop()?.replace(/^NVIDIA-/i, "") || model;
  return model.toLowerCase().startsWith("nvidia/") ? `NVIDIA ${name}` : name;
}
