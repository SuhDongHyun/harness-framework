const $ = (id) => document.getElementById(id);
let sequence = 0;
let liveEvents = [];
let initialized = false;
let renderedPlan = "";
let approvalCommand = "";
let copyResetTimer;

const text = (node, value) => { node.textContent = value ?? "—"; };
const formatNumber = (value) => Number(value || 0).toLocaleString();
const eventType = (entry) => entry?.event?.type || entry?.type || "event";

function renderList(node, pairs) {
  node.replaceChildren();
  for (const [label, value, className] of pairs) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    if (className) dd.className = className;
    node.append(dt, dd);
  }
}

function renderSteps(data) {
  const node = $("steps");
  node.replaceChildren();
  const stateSteps = data.state?.steps || [];
  for (const [index, step] of stateSteps.entries()) {
    const row = document.createElement("div");
    row.className = `step ${step.status}` + (data.state?.current_step === step.id ? " active" : "");
    const number = document.createElement("span");
    number.className = "mono";
    number.textContent = String(index + 1).padStart(2, "0");
    const name = document.createElement("span");
    name.textContent = step.name;
    const dot = document.createElement("span");
    dot.className = "state-dot";
    dot.textContent = step.status === "completed" ? "✓" : step.status === "failed" || step.status === "blocked" ? "!" : "";
    row.append(number, name, dot);
    node.append(row);
  }
}

function formatCommand(command) {
  if (!Array.isArray(command)) return String(command ?? "");
  return command.map((part) => {
    const value = String(part);
    return /^[A-Za-z0-9_./:=+,-]+$/.test(value) ? value : JSON.stringify(value);
  }).join(" ");
}

function appendPlanList(node, label, values, commandList = false) {
  const section = document.createElement("section");
  section.className = "plan-field";
  const title = document.createElement("h4");
  title.textContent = label;
  const list = document.createElement("ul");
  if (!Array.isArray(values) || !values.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "None";
    list.append(item);
  } else {
    for (const value of values) {
      const item = document.createElement("li");
      const content = document.createElement(commandList ? "code" : "span");
      content.textContent = commandList ? formatCommand(value) : String(value);
      item.append(content);
      list.append(item);
    }
  }
  section.append(title, list);
  node.append(section);
}

function fallbackCopy(value) {
  const input = document.createElement("textarea");
  input.className = "copy-source";
  input.value = value;
  input.setAttribute("readonly", "");
  document.body.append(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("clipboard copy was rejected");
}

async function copyApprovalCommand() {
  if (!approvalCommand) return;
  const button = $("copy-approval");
  const feedback = $("copy-feedback");
  clearTimeout(copyResetTimer);
  button.disabled = true;
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(approvalCommand);
      } catch (error) {
        fallbackCopy(approvalCommand);
      }
    } else {
      fallbackCopy(approvalCommand);
    }
    button.textContent = "COPIED";
    feedback.textContent = "Paste the command into your Codex conversation.";
  } catch (error) {
    button.textContent = "COPY FAILED";
    feedback.textContent = `Copy manually: ${approvalCommand}`;
  }
  copyResetTimer = setTimeout(() => {
    button.textContent = approvalCommand ? "COPY APPROVAL COMMAND" : "APPROVAL UNAVAILABLE";
    button.disabled = !approvalCommand;
    feedback.textContent = "";
  }, 1800);
}

function renderPlan(data) {
  const plan = data.plan || {};
  const signature = JSON.stringify(plan);
  if (signature === renderedPlan) return;
  renderedPlan = signature;
  text($("plan-goal"), plan.goal || "Plan is not available yet");
  text($("plan-status"), String(plan.status || data.state?.status || "waiting").toUpperCase());
  const approvalButton = $("copy-approval");
  const approvable = data.state?.status === "draft" && Boolean(data.run_id);
  approvalCommand = approvable ? `$harness-approve ${data.run_id}` : "";
  approvalButton.disabled = !approvable;
  approvalButton.textContent = approvable ? "COPY APPROVAL COMMAND" : "APPROVAL UNAVAILABLE";
  approvalButton.title = approvalCommand || "Only draft runs can be approved";
  const stepsNode = $("plan-steps");
  stepsNode.replaceChildren();
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  for (const [index, step] of steps.entries()) {
    const details = document.createElement("details");
    details.className = "plan-step";
    details.open = index === 0;
    const summary = document.createElement("summary");
    const identifier = document.createElement("span");
    identifier.className = "mono accent";
    identifier.textContent = step.id || String(index + 1).padStart(2, "0");
    const name = document.createElement("strong");
    name.textContent = step.name || "Unnamed step";
    const dependency = document.createElement("span");
    dependency.className = "dependency mono";
    dependency.textContent = Array.isArray(step.depends_on) && step.depends_on.length
      ? `after ${step.depends_on.join(", ")}` : "no dependencies";
    summary.append(identifier, name, dependency);

    const body = document.createElement("div");
    body.className = "plan-step-body";
    const objective = document.createElement("p");
    objective.className = "objective";
    objective.textContent = step.objective || "No objective provided";
    const fields = document.createElement("div");
    fields.className = "plan-fields";
    appendPlanList(fields, "READ FILES", step.read_files);
    appendPlanList(fields, "ALLOWED PATHS", step.allowed_paths);
    appendPlanList(fields, "FORBIDDEN CHANGES", step.forbidden_changes);
    appendPlanList(fields, "ACCEPTANCE COMMANDS", step.acceptance_commands, true);
    body.append(objective, fields);
    details.append(summary, body);
    stepsNode.append(details);
  }

  const finalCommands = $("final-acceptance-commands");
  finalCommands.replaceChildren();
  const commands = Array.isArray(plan.final_acceptance_commands)
    ? plan.final_acceptance_commands : [];
  for (const command of commands) {
    const item = document.createElement("li");
    const code = document.createElement("code");
    code.textContent = formatCommand(command);
    item.append(code);
    finalCommands.append(item);
  }
  if (!commands.length) {
    const item = document.createElement("li");
    item.className = "muted";
    item.textContent = "None";
    finalCommands.append(item);
  }
}

function normalizeEvents(data) {
  const historical = (data.historical_agent_events || []).map((item) => ({
    event: item.event, source: item.source
  }));
  const incoming = data.live?.events || [];
  if (incoming.length) liveEvents = [...liveEvents, ...incoming].slice(-120);
  const selected = liveEvents.length ? liveEvents : historical.slice(-120);
  return selected.filter((item) => {
    const type = eventType(item);
    return type.startsWith("item.") || type.startsWith("turn.") || type === "error";
  }).slice(-18).reverse();
}

function describeEvent(item) {
  const event = item.event || {};
  const type = event.type || "event";
  const detail = event.item || {};
  let title = type;
  let body = "";
  if (detail.type === "command_execution" || detail.type === "commandExecution") {
    title = detail.command || "command execution";
    body = detail.aggregated_output || detail.aggregatedOutput || "";
  } else if (detail.type === "collabToolCall") {
    title = `${detail.tool || "subagent"} · ${detail.agentStatus || detail.status || "running"}`;
  } else if (detail.type === "agent_message" || detail.type === "agentMessage") {
    title = "agent message";
    body = detail.text || "";
  } else if (detail.type) {
    title = detail.type.replaceAll("_", " ");
  }
  const status = detail.status || (type.endsWith("completed") ? "completed" : type.endsWith("started") ? "running" : type === "error" ? "failed" : "pending");
  return { title, body: String(body).slice(-1000), status };
}

function renderActivity(data) {
  const node = $("activity-list");
  node.replaceChildren();
  const events = normalizeEvents(data);
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "event pending";
    empty.textContent = "Waiting for agent activity";
    node.append(empty);
    return;
  }
  for (const item of events) {
    const value = describeEvent(item);
    const row = document.createElement("article");
    row.className = `event ${value.status}`;
    const dot = document.createElement("i"); dot.className = "event-dot";
    const content = document.createElement("div");
    const title = document.createElement("div"); title.className = "event-title"; title.textContent = value.title;
    content.append(title);
    if (value.body) { const body = document.createElement("div"); body.className = "event-detail"; body.textContent = value.body; content.append(body); }
    const meta = document.createElement("span"); meta.className = "event-meta"; meta.textContent = value.status;
    row.append(dot, content, meta); node.append(row);
  }
}

function render(data) {
  const state = data.state || {};
  const steps = state.steps || [];
  const completed = steps.filter((step) => step.status === "completed").length;
  const percent = steps.length ? Math.round((completed / steps.length) * 100) : 0;
  text($("run-id"), data.run_id);
  text($("run-status"), `${String(state.status || "loading").toUpperCase()} ${percent}%`);
  $("run-bar").style.width = `${percent}%`;
  text($("branch"), data.branch ? `⌘ ${data.branch}` : "branch —");
  const current = steps.find((step) => step.id === state.current_step);
  text($("current-step"), current ? `${current.id} · attempt ${current.attempts}` : state.status || "waiting");
  renderSteps(data);
  renderPlan(data);
  renderActivity(data);
  const profile = data.profiles?.executor || {};
  renderList($("run-metrics"), [
    ["Model", profile.model || "—"], ["Effort", profile.reasoning_effort || "—"],
    ["Attempts", String(steps.reduce((sum, step) => sum + Number(step.attempts || 0), 0))],
    ["Files changed", String((data.changed_files || []).length)]
  ]);
  const usage = data.usage || {};
  renderList($("token-usage"), [
    ["Input", formatNumber(usage.input_tokens)], ["Cached", formatNumber(usage.cached_input_tokens)],
    ["Output", formatNumber(usage.output_tokens)], ["Reasoning", formatNumber(usage.reasoning_output_tokens)]
  ]);
  const terminal = ["completed", "failed", "blocked"].includes(state.status);
  const guardStatus = state.status === "completed" ? "PASS" : terminal ? "CHECK" : state.status === "draft" || state.status === "approved" ? "WAITING" : "MONITORING";
  const guardClass = state.status === "completed" ? "pass" : terminal ? "fail" : "running";
  renderList($("verification"), [
    ["Git scope", guardStatus, guardClass],
    ["Index / HEAD", guardStatus, guardClass],
    ["Acceptance", state.status === "completed" ? "PASS" : terminal ? String(state.status).toUpperCase() : "RUNNING", state.status === "completed" ? "pass" : terminal ? "fail" : "running"]
  ]);
  text($("latest-output"), data.latest_output);
  sequence = Math.max(sequence, Number(data.live?.sequence || 0));
}

async function refresh() {
  try {
    const response = await fetch(`/api/snapshot?after=${sequence}&initial=${initialized ? 0 : 1}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    initialized = true;
    $("connection").textContent = "LIVE";
    $("connection").className = "connection live";
  } catch (error) {
    $("connection").textContent = "RECONNECTING";
    $("connection").className = "connection";
  }
}

setInterval(() => text($("clock"), new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})), 1000);
$("copy-approval").addEventListener("click", copyApprovalCommand);
refresh();
setInterval(refresh, 750);
