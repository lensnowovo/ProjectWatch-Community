import { uploadBatch } from "./upload_batch.mjs";
import { createClientShell } from "./client_shell.mjs";
import { primaryView, contentView } from "./navigation.mjs";
import { readingNews } from "./external_attention.mjs";
import { captureReturnFocus } from "./return_focus.mjs";
import { materialSelection } from "./page_polish.mjs";

const $ = (id) => document.getElementById(id);
const PROJECT_PAGE_SIZE = 50;
const signedValue = (value) => Number(value) > 0 ? `+${value}` : String(value);
const state = { projects: [], projectHasMore: false, current: null, view: "overview", sources: [], jobs: [], observations: [], impacts: [], holdingSnapshots: [], holdingDraft: null, holdingReview: null, watchItems: [], watchChanges: [], watchError: null, watchReview: null, watchChange: null, watchChangeReview: null, metricSeries: null, metricError: null, metricDraft: null, financialScreen: null, financialError: null, monitoringDomains: null, domainsError: null, competition: null, competitionError: null, monitoringPlans: [], monitoringJobs: [], signals: [], monitoringError: null, publicSearchPreviews: null, coverageTask: null, coverageError: false, runKeys: new Map(), selectedSource: null, selectedCandidates: new Set(), batchDecisions: new Map(), review: null, impactReview: null, timer: null, teamMode: false, teamSession: null, members: [], memberHasMore: false, memberProjectId: null, memberRevoke: null, identity: null, identityProjectId: null, capabilities: { read: false, propose: false, review: false, manage: false } };
let uploadActive = false;
let privateLocalIntake = false;
let aiExtraction = null;
let aiExtractionBusy = false;
let readerReturn = null;
state.screen = "home";
let client;

async function openAiExtraction(source, fragment) {
  if (aiExtractionBusy) { toast("已有一条 AI 提取正在执行"); return; }
  const projectId = state.current.id;
  try {
    const params = new URLSearchParams({ source_id: source.id, source_locator: fragment.locator });
    const preview = await request(`/api/projects/${projectId}/ai-extraction?${params}`);
    if (state.current?.id !== projectId) return;
    aiExtraction = { projectId, ticket: preview.ticket };
    $("ai-extraction-form").reset();
    $("ai-extraction-destination").textContent = `${preview.executor} · ${preview.model} · ${preview.base_url} · 本进程剩余 ${preview.remaining_runs} 次`;
    $("ai-extraction-source").textContent = `${preview.source_reference} · ${preview.source_locator}${preview.truncated ? " · 仅前 1500 字符" : ""}`;
    $("ai-extraction-excerpt").textContent = preview.excerpt;
    $("ai-extraction-status").textContent = "";
    $("ai-extraction-submit").disabled = preview.remaining_runs <= 0;
    $("ai-extraction-dialog").showModal();
  } catch (error) { toast(error.message); }
}

$("ai-extraction-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!aiExtraction || aiExtractionBusy || !$("ai-extraction-consent").checked) return;
  const selected = aiExtraction;
  aiExtractionBusy = true; $("ai-extraction-submit").disabled = true;
  $("ai-extraction-status").textContent = "正在提取，通常需要几十秒；关闭窗口不会取消已发出的请求。";
  try {
    const result = await request(`/api/projects/${selected.projectId}/ai-extraction`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket: selected.ticket, consent: true }),
    });
    $("ai-extraction-status").textContent = `${result.model} 已提交一条待核对记录。请在待核对清单中检查原文和口径。`;
    if (state.current?.id === selected.projectId) await refreshProject();
    toast("AI 候选已加入待核对清单");
  } catch (error) {
    $("ai-extraction-status").textContent = `${error.message} 若连接中断，请先检查待核对清单；系统不会自动重试。`;
  } finally { aiExtractionBusy = false; aiExtraction = null; }
});

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.teamSession && ["POST", "PUT", "PATCH", "DELETE"].includes(options.method?.toUpperCase())) headers.set("X-CSRF-Token", state.teamSession.csrf_token);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (response.status === 401 && state.teamMode && path !== "/api/auth/login") showLogin();
  if (!response.ok) { const error = new Error(data?.detail || `请求失败（${response.status}）`); error.status = response.status; throw error; }
  return data;
}
function showLogin() {
  $("upload-status").hidden = true;
  closeMemberDialogs();
  closeIdentityDialog();
  state.teamSession = null;
  state.current = null; state.projects = []; state.projectHasMore = false; state.sources = []; state.jobs = []; state.observations = []; state.impacts = [];
  state.watchItems = []; state.watchChanges = []; state.watchError = null; state.watchReview = null; state.watchChange = null; state.watchChangeReview = null;
  state.holdingSnapshots = []; state.holdingDraft = null; state.holdingReview = null;
  state.metricSeries = null; state.metricError = null; state.metricDraft = null;
  state.financialScreen = null; state.financialError = null;
  state.monitoringDomains = null; state.domainsError = null;
  state.competition = null; state.competitionError = null;
  state.monitoringPlans = []; state.monitoringJobs = []; state.signals = []; state.coverageTask = null;
  state.selectedSource = null; state.selectedCandidates.clear(); state.batchDecisions.clear(); state.review = null; state.impactReview = null;
  state.capabilities = { read: false, propose: false, review: false, manage: false };
  clearTimeout(state.timer);
  sessionStorage.removeItem("project-watch-current");
  $("reader").hidden = true;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  renderProjects(); applyMode();
  $("login-screen").hidden = false;
  $("login-username").focus();
}
function applyMode() {
  if (!state.teamMode) return;
  $("environment-label").textContent = `${state.teamSession?.username || "团队模式"} · 团队研究`;
  $("logout-button").hidden = !state.teamSession;
  const canCreate = Boolean(state.teamSession?.global_admin);
  $("new-project").hidden = !canCreate;
  $("welcome-create").hidden = !canCreate;
  $("preview-reviewer").hidden = true;
  $("reviewer-input").required = false;
  $("team-reviewer").hidden = false;
  $("team-reviewer").textContent = `审核人：${state.teamSession?.username || ""}`;
  $("impact-preview-reviewer").hidden = true;
  $("impact-reviewer").required = false;
  $("impact-team-reviewer").hidden = false;
  $("impact-team-reviewer").textContent = `复核人：${state.teamSession?.username || ""}`;
  $("batch-preview-reviewer").hidden = true;
  $("batch-reviewer-input").required = false;
  $("batch-team-reviewer").hidden = false;
  $("batch-team-reviewer").textContent = `审核人：${state.teamSession?.username || ""}`;
  $("watch-preview-reviewer").hidden = true;
  $("watch-reviewer").required = false;
  $("watch-team-reviewer").hidden = false;
  $("watch-team-reviewer").textContent = `审核人：${state.teamSession?.username || ""}`;
  $("watch-change-preview-reviewer").hidden = true;
  $("watch-change-reviewer").required = false;
  $("watch-change-team-reviewer").hidden = false;
  $("watch-change-team-reviewer").textContent = `审核人：${state.teamSession?.username || ""}`;
  $("drop-overlay").querySelector("small").textContent = "材料将保存在当前机构的自托管空间";
  document.querySelector("#monitoring-dialog .preview-warning").textContent = "仅适合公开、无需登录的订阅源。";
  $("welcome").querySelector("h1").textContent = canCreate ? "从一份材料，开始了解一家公司。" : "当前没有可进入的项目。";
  $("welcome").querySelector(".welcome-copy").textContent = canCreate ? "建立项目后，材料在机构的自托管空间排队解析。事实和判断仍由团队成员确认。" : "请项目管理员将你加入项目。加入后，项目会出现在左侧列表中。";
  $("header-upload").closest(".upload-note").querySelector("h3").textContent = "新材料到了？";
  $("header-upload").closest(".upload-note").querySelector("p").textContent = "拖进此页即可建立来源版本并开始解析；原件保留在机构的自托管空间。";
  $("header-upload").textContent = "选择文件 →";
  applyCapabilities();
}
function applyCapabilities() {
  const { propose, manage } = state.capabilities;
  $("project-members").hidden = !state.teamMode || !manage;
  if (!manage) closeMemberDialogs();
  for (const id of ["header-upload", "material-upload", "run-coverage"]) $(id).hidden = !propose;
  $("header-upload").closest(".upload-note").hidden = !propose;
  $("add-monitoring").hidden = !manage;
  $("watch-create").hidden = !propose;
  $("public-search-starter").hidden = !manage;
}
async function bootstrap() {
  const response = await fetch("/api/auth/session", { credentials: "same-origin" });
  if (response.status === 404) {
    const runtime = await request("/api/runtime");
    privateLocalIntake = runtime.private_local_intake === true;
    if (privateLocalIntake) {
      $("environment-label").textContent = "资料保存在本机";
      document.querySelector(".welcome-copy").textContent = "填写项目名称，或添加材料。原件保存在本机，解析结果进入待核对区。";
      const uploadNote = $("header-upload").closest(".upload-note");
      uploadNote.querySelector("h3").textContent = "新材料到了？";
      uploadNote.querySelector("p").textContent = "拖入文件即可隔离解析、归档和生成候选。不会自动上传机构 DB，也不会自动发送给云端模型。";
      $("header-upload").textContent = "选择文件 →";
      $("drop-overlay").querySelector("small").textContent = "保存在本机 · 隔离解析 · 结果待核对";
    }
    await loadProjects();
    return;
  }
  state.teamMode = true;
  if (response.status === 401) { showLogin(); return; }
  if (!response.ok) throw new Error(`身份检查失败（${response.status}）`);
  state.teamSession = await response.json();
  applyMode();
  await loadProjects();
}
function dateText(value) { return value ? new Date(value).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" }) : ""; }
function toast(message) {
  const box = $("toast"); box.textContent = message; box.hidden = false;
  clearTimeout(box._timer); box._timer = setTimeout(() => { box.hidden = true; }, 4400);
}
function empty(container, message) { const line = document.createElement("p"); line.className = "empty"; line.textContent = message; container.replaceChildren(line); }
function makeRow(className, title, subtitle, action, onClick) {
  const row = document.createElement("button"); row.type = "button"; row.className = className;
  const text = document.createElement("span");
  const strong = document.createElement("strong"); strong.textContent = title;
  const small = document.createElement("small"); small.textContent = subtitle;
  text.append(strong, small);
  const label = document.createElement("span"); label.className = "row-action"; label.textContent = action;
  row.append(text, label); row.addEventListener("click", onClick);
  return row;
}
function projectButton(project) {
  const button = document.createElement("button"); button.type = "button"; button.className = "project-link";
  if (project.id === state.current?.id) button.setAttribute("aria-current", "page");
  const name = document.createElement("span"); name.textContent = project.name;
  const arrow = document.createElement("small"); arrow.textContent = "↗";
  button.append(name, arrow); button.addEventListener("click", () => client.navigate(project.id));
  return button;
}
function renderProjects() {
  const visible = state.projects.filter(p => state.teamMode || !/合成|synthetic|演示|demo/i.test(p.name));
  $("project-count").textContent = `${visible.length}${state.projectHasMore ? "+" : ""}`;
  const recent = [...visible].sort((a,b) => Number(state.pinnedIds?.has(b.id)||false)-Number(state.pinnedIds?.has(a.id)||false));
  const query = $("project-directory-search").value.trim().toLocaleLowerCase();
  const matches = recent.filter(project => project.name.toLocaleLowerCase().includes(query));
  $("project-list").replaceChildren(...matches.slice(0,query ? 50 : 8).map(projectButton));
  if(query && !matches.length) empty($("project-list"),"没有找到项目");
  $("project-more").hidden = !state.projectHasMore;
  $("welcome").hidden = Boolean(state.current);
  $("project-screen").hidden = !state.current;
  client?.sync();
}
async function loadProjects(select = true) {
  state.projects = await request(`/api/projects?limit=${PROJECT_PAGE_SIZE}`);
  state.projectHasMore = state.projects.length === PROJECT_PAGE_SIZE;
  if (!select) { renderProjects(); return; }
  if (!state.teamMode) { renderProjects(); await client.start(); return; }
  const saved = sessionStorage.getItem("project-watch-current");
  const selection = state.projects.find((project) => project.id === saved) || state.projects[0];
  if (selection) await selectProject(selection.id); else renderProjects();
}
async function loadMoreProjects() {
  if (!state.projectHasMore || !state.projects.length) return;
  const button = $("project-more"); button.disabled = true;
  try {
    const last = state.projects.at(-1);
    const params = new URLSearchParams({ limit: String(PROJECT_PAGE_SIZE), after_created_at: last.created_at, after_id: last.id });
    const page = await request(`/api/projects?${params}`);
    const known = new Set(state.projects.map((item) => item.id));
    state.projects.push(...page.filter((item) => !known.has(item.id)));
    state.projectHasMore = page.length === PROJECT_PAGE_SIZE;
    renderProjects();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
}
async function selectProject(id) {
  const project = state.projects.find((item) => item.id === id);
  if (!project) return;
  state.projectRevision = (state.projectRevision || 0) + 1;
  clearTimeout(state.timer);
  state.screen = "project";
  closeMemberDialogs();
  closeIdentityDialog();
  state.current = project; state.selectedSource = null; state.selectedCandidates.clear(); state.batchDecisions.clear(); $("reader").hidden = true;
  if ($("batch-review-dialog").open) $("batch-review-dialog").close();
  state.capabilities = { read: false, propose: false, review: false, manage: false };
  state.sources = []; state.jobs = []; state.observations = []; state.impacts = [];
  state.reading = null; state.readingError = null;
  state.externalAnalysis = null; state.externalAnalysisError = null;
  state.external = null; state.externalError = null;
  state.briefing = null; state.briefingError = null;
  state.profile = null; state.profileError = null;
  state.holdingSnapshots = []; state.holdingDraft = null; state.holdingReview = null;
  state.metricSeries = null; state.metricError = null; state.metricDraft = null;
  state.financialScreen = null; state.financialError = null;
  state.monitoringDomains = null; state.domainsError = null;
  state.competition = null; state.competitionError = null;
  state.watchItems = []; state.watchChanges = []; state.watchError = null;
  state.watchReview = null; state.watchChange = null; state.watchChangeReview = null;
  state.publicSearchPreviews = null; state.review = null; state.impactReview = null;
  state.monitoringPlans = []; state.monitoringJobs = []; state.signals = []; state.monitoringError = null;
  state.coverageTask = null; state.coverageError = false;
  $("report-content").replaceChildren(reportNode("p", "muted", "正在读取当前项目简报…"));
  sessionStorage.setItem("project-watch-current", id);
  renderProjects(); renderView(); applyCapabilities(); renderProject();
  await refreshProject();
  if (state.current?.id === id && state.view === "report") await loadReport();
}
function closeMemberDialogs() {
  for (const id of ["member-revoke-dialog", "members-dialog"]) if ($(id).open) $(id).close();
  $("member-create-form").reset();
  $("member-assign-form").reset();
  $("members-list").replaceChildren();
  state.members = []; state.memberHasMore = false; state.memberProjectId = null; state.memberRevoke = null;
}
function closeIdentityDialog() {
  if ($("identity-dialog").open) $("identity-dialog").close();
  $("identity-verify-form").reset(); $("identity-reopen-form").reset();
  state.identity = null; state.identityProjectId = null;
}
function identityVersion() { return state.identity?.history?.[0]?.version || 0; }
function renderIdentity() {
  const box = $("identity-current"); box.replaceChildren();
  const history = $("identity-history"); history.replaceChildren();
  if (!state.identity) { empty(box, "正在读取主体核对记录…"); return; }
  const latest = state.identity.history[0];
  const verified = state.identity.status === "verified" && latest?.action === "verify";
  const title = document.createElement("strong");
  title.textContent = verified ? latest.legal_name : "尚未核对法律主体";
  box.append(title);
  const detail = document.createElement("p");
  detail.textContent = verified ? `${latest.registration_code ? `登记代码 ${latest.registration_code} · ` : ""}由团队成员人工核对 · ${dateText(latest.created_at)}` : "项目名称只是研究入口；公开线索中的同名主体还需人来判断。";
  box.append(detail);
  if (verified) {
    const cite = document.createElement("p");
    const source = state.sources.find((item) => item.id === latest.evidence_source_id);
    cite.textContent = latest.evidence_url ? `依据：${latest.evidence_url}（仅保存引用，系统未抓取）` : `依据：${source?.reference || "已登记原件"} · ${latest.evidence_locator}`;
    box.append(cite);
  }
  if (state.identity.history.length) {
    const heading = document.createElement("h3"); heading.textContent = "核对记录"; history.append(heading);
    for (const item of state.identity.history) {
      const entry = document.createElement("article"); entry.className = "identity-history-entry";
      const action = document.createElement("strong"); action.textContent = item.action === "verify" ? `核对主体 · ${item.legal_name}` : "重新打开核对";
      const when = document.createElement("span"); when.textContent = `${dateText(item.created_at)} · ${item.actor_name || "团队账号"}`;
      const note = document.createElement("p"); note.textContent = item.note;
      entry.append(action, when, note); history.append(entry);
    }
  }
  const canManage = state.teamMode && state.capabilities.manage;
  $("identity-readonly").hidden = canManage;
  $("identity-readonly").textContent = state.teamMode ? "仅项目管理员可记录主体核对。" : "本机预览不具备已验证的审核身份；请在团队模式记录正式核对。";
  $("identity-verify-form").hidden = !canManage || verified;
  $("identity-reopen-form").hidden = !canManage || !verified;
}
async function loadIdentity() {
  const projectId = state.identityProjectId;
  if (!projectId || projectId !== state.current?.id) return;
  const result = await request(`/api/projects/${projectId}/identity`);
  if (!$("identity-dialog").open || projectId !== state.current?.id) return;
  state.identity = result; renderIdentity();
}
function renderIdentityEvidenceType() {
  const sourceMode = $("identity-evidence-type").value === "source";
  $("identity-url-fields").hidden = sourceMode;
  $("identity-source-fields").hidden = !sourceMode;
  $("identity-evidence-url").required = !sourceMode;
  $("identity-source").required = sourceMode;
  $("identity-locator").required = sourceMode;
}
async function loadIdentityLocators() {
  const sourceId = $("identity-source").value;
  const projectId = state.identityProjectId;
  const select = $("identity-locator"); select.replaceChildren();
  if (!sourceId || !projectId || projectId !== state.current?.id) return;
  const fragments = await request(`/api/projects/${projectId}/sources/${sourceId}/fragments`);
  if (!$("identity-dialog").open || projectId !== state.current?.id || sourceId !== $("identity-source").value) return;
  const seen = new Set();
  for (const fragment of fragments) {
    if (seen.has(fragment.locator)) continue;
    seen.add(fragment.locator);
    const option = document.createElement("option"); option.value = fragment.locator;
    option.textContent = `${fragment.locator} · ${fragment.text.slice(0, 56).replace(/\s+/g, " ")}`;
    select.append(option);
  }
}
function renderMembers() {
  const box = $("members-list"); box.replaceChildren();
  if (!state.members.length) empty(box, "尚未分配项目成员。");
  for (const member of state.members) {
    const row = document.createElement("div"); row.className = "member-row";
    const identity = document.createElement("div"); identity.className = "member-identity";
    const name = document.createElement("strong"); name.textContent = member.username;
    const status = document.createElement("small"); status.textContent = member.active ? "团队账号正常" : "账号已停用";
    identity.append(name, status);
    const actions = document.createElement("div"); actions.className = "member-actions";
    const role = document.createElement("select"); role.setAttribute("aria-label", member.username + " 的项目权限");
    for (const [value, label] of [["viewer", "查看"], ["analyst", "分析"], ["reviewer", "审核"], ["admin", "项目管理"]]) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; role.append(option);
    }
    role.value = member.role; role.disabled = !member.active;
    const save = document.createElement("button"); save.type = "button"; save.className = "text-button"; save.textContent = "保存"; save.disabled = !member.active;
    save.addEventListener("click", async () => {
      if (!state.memberProjectId || state.memberProjectId !== state.current?.id) return;
      save.disabled = true;
      try {
        await request("/api/projects/" + state.memberProjectId + "/members/" + member.user_id, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role: role.value }),
        });
        await loadMembers(true); toast("项目权限已更新");
      } catch (error) { toast(error.message); } finally { save.disabled = false; }
    });
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "text-button member-remove"; remove.textContent = "移除";
    remove.addEventListener("click", () => {
      if (!state.memberProjectId || state.memberProjectId !== state.current?.id) return;
      state.memberRevoke = { projectId: state.memberProjectId, userId: member.user_id };
      $("member-revoke-name").textContent = "确定移除 " + member.username + " 在当前项目的权限？";
      $("member-revoke-dialog").showModal();
    });
    actions.append(role, save, remove); row.append(identity, actions); box.append(row);
  }
  $("members-more").hidden = !state.memberHasMore;
}
async function loadMembers(reset = false) {
  const id = state.memberProjectId;
  if (!id || id !== state.current?.id || !state.capabilities.manage) return;
  const params = new URLSearchParams({ limit: "100" });
  if (!reset && state.members.length) params.set("after_username", state.members.at(-1).username);
  const page = await request("/api/projects/" + id + "/members?" + params);
  if (id !== state.current?.id || !$("members-dialog").open) return;
  state.members = reset ? page : [...state.members, ...page];
  state.memberHasMore = page.length === 100;
  renderMembers();
}
function renderView() {
  document.querySelectorAll('.content-body > .view').forEach(v => {v.hidden = v.id !== 'view-' + contentView(state.view);});
  $("internal-navigation").hidden = primaryView(state.view) !== 'internal';
  for (const button of document.querySelectorAll("[data-view]")) {
    const active = button.hasAttribute('data-primary') ? button.dataset.view === primaryView(state.view) : button.dataset.view === contentView(state.view);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  client?.tabChanged();
}
function reportNode(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function reportSection(title) {
  const section = reportNode("section", "report-section");
  section.append(reportNode("h3", "", title));
  return section;
}
function reviewProofText(proof) {
  if (!proof) return "审核记录未找到 · 请返回事实核对页核实";
  const reviewer = proof.reviewer_authentication === "authenticated_team" ? "团队审核账号" : "本机预览自报审核人（未认证）";
  return `${reviewer} ${proof.reviewer_ref} · ${dateText(proof.decided_at)} · 审核记录 ${proof.decision_id}`;
}
function locatorStatusText(status) {
  return ({
    indexed_exact: "定位已匹配提取片段（内容仍需人工核对）",
    locator_not_found: "定位未在已提取片段中找到",
    not_indexed: "原件尚无可核对的提取索引",
    verification_unavailable: "定位核对状态暂不可用",
  })[status] || "定位核对状态暂不可用";
}
function renderReport(brief) {
  const root = $("report-content"); root.replaceChildren();
  const header = reportNode("header", "report-cover");
  header.append(reportNode("p", "report-kicker", "PROJECT WATCH  /  MONITORING BRIEF"));
  header.append(reportNode("h2", "", brief.project_name));
  header.append(reportNode("p", "report-deck", "项目监控 · 当期简报"));
  header.append(reportNode("p", "report-meta", `截至 ${dateText(brief.as_of)} · 内部工作底稿 · ${brief.identity_status === "verified" ? "主体已核验" : "主体未核验"}`));
  root.append(header);
  const lead = reportSection("本期概览");
  const stats = reportNode("div", "report-stats");
  for (const [value, label] of [[brief.new_source_count, "近 30 天新增材料"], [brief.confirmed_count, "已确认观察"], [brief.candidate_count, "待核对候选"], [brief.pending_review_count, "待回看旧判断"]]) {
    const item = reportNode("div", ""); item.append(reportNode("strong", "", String(value)), reportNode("span", "", label)); stats.append(item);
  }
  lead.append(stats);
  lead.append(reportNode("p", "report-caveat", "新增材料按来源入库时间统计，不代表企业经营变化；没有记录也不代表没有风险。"));
  root.append(lead);
  const holdings = reportSection("本机构持仓");
  holdings.append(reportNode("p", "report-caveat", "逐基金列示经人工确认的最新时点，不跨基金汇总；公司融资不等于本机构持仓。"));
  for (const position of brief.holdings?.positions || []) {
    const row = reportNode("div", "report-fact");
    row.append(reportNode("strong", "", `${position.fund_name} · ${position.as_of}`));
    row.append(reportNode("p", "", [
      position.invested_cost_wan === null ? "成本未登记" : `投资成本 ${position.invested_cost_wan} 万元`,
      position.ownership_pct === null ? "持股未登记" : `持股 ${position.ownership_pct}%`,
      position.current_value_wan === null ? "当前价值未登记" : `当前价值 ${position.current_value_wan} 万元`,
      position.moc === null ? "MOC 未计算" : `MOC ${position.moc}x`,
    ].join(" · ")));
    const source = reportNode("button", "report-source text-button", `${position.source_locator} · SHA ${position.source_sha256.slice(0, 12)} ↗`);
    source.type = "button";
    source.addEventListener("click", () => openSource(state.sources.find((item) => item.id === position.source_id && item.content_sha256 === position.source_sha256), position.source_locator));
    row.append(source, reportNode("small", "report-review-proof", `版本 ${position.version} · 审核人 ${position.reviewed_by || "未记录"}`));
    if (position.new_material_count_since_review) row.append(reportNode("small", "report-caveat", `审核后新增 ${position.new_material_count_since_review} 份项目材料，尚未核对是否影响这笔持仓。`));
    holdings.append(row);
  }
  client?.tabChanged();
  client?.sync();
  for (const conflict of brief.holdings?.conflicts || []) {
    holdings.append(reportNode("p", "report-caveat", `${conflict.fund_name} · ${conflict.as_of} 同期有多条已确认快照，暂不展示数值，需复核。`));
  }
  if (brief.holdings?.pending_count) holdings.append(reportNode("p", "report-caveat", `${brief.holdings.pending_count} 条持仓快照待核对或存在冲突。`));
  if (!brief.holdings?.positions?.length && !brief.holdings?.conflicts?.length) holdings.append(reportNode("p", "report-empty", "暂无可展示的已确认持仓。"));
  root.append(holdings);
  const newMaterials = reportSection("近 30 天新增材料");
  const recentSources = brief.new_sources || [];
  if (!recentSources.length) newMaterials.append(reportNode("p", "report-empty", "本窗口暂无新材料；不据此判断企业没有变化。"));
  for (const item of recentSources) {
    const source = reportNode("button", "report-material text-button", `${item.reference} · ${dateText(item.created_at)} · SHA ${item.source_sha256.slice(0, 12)} ↗`);
    source.type = "button";
    source.addEventListener("click", () => openSource(state.sources.find((row) => row.id === item.source_id)));
    newMaterials.append(source);
  }
  if (brief.new_sources_truncated) newMaterials.append(reportNode("p", "report-caveat", "只展示最近 10 份；完整清单见项目材料页。"));
  root.append(newMaterials);
  const reviewImpacts = reportSection("新材料与旧判断回看");
  if (!brief.review_impacts?.length) reviewImpacts.append(reportNode("p", "report-empty", "本期没有待回看的旧判断或近期回看记录。"));
  for (const impact of brief.review_impacts || []) {
    const row = reportNode("div", "report-impact");
    const reviewState = impact.status === "pending" ? "待回看 · 不代表内容冲突" : impact.resolution === "unchanged" ? "已回看 · 原判断保留" : "已回看 · 需继续查证";
    row.append(reportNode("p", "report-impact-status", `${impact.topic} · ${reviewState}`));
    row.append(reportNode("strong", "", impact.old_statement));
    const sources = reportNode("div", "report-impact-sources");
    const oldSource = reportNode("button", "report-source text-button", `旧判断：${impact.old_source_reference} · ${impact.old_source_locator} · 版本 ${impact.old_observation_version} · SHA ${impact.old_source_sha256.slice(0, 12)} ↗`);
    oldSource.type = "button";
    oldSource.addEventListener("click", () => openSource(state.sources.find((item) => item.id === impact.old_source_id), impact.old_source_locator));
    const newSource = reportNode("button", "report-source text-button", `新材料：${impact.new_source_reference} · SHA ${impact.new_source_sha256.slice(0, 12)} ↗`);
    newSource.type = "button";
    newSource.addEventListener("click", () => openSource(state.sources.find((item) => item.id === impact.new_source_id)));
    sources.append(oldSource, newSource); row.append(sources);
    if (impact.status === "reviewed") row.append(reportNode("p", "report-caveat", `${impact.note || "未填写回看说明"} · ${impact.reviewer_ref || "审核人未记录"} · ${dateText(impact.reviewed_at)}`));
    reviewImpacts.append(row);
  }
  if (brief.review_impacts_truncated) reviewImpacts.append(reportNode("p", "report-caveat", "仅展示前 20 条；完整回看记录见事实核对页。"));
  root.append(reviewImpacts);
  const watchSection = reportSection("持续关注与下一步");
  if (!brief.watch_items?.length) watchSection.append(reportNode("p", "report-empty", "暂无经审核的关注事项；不代表没有风险或待办。"));
  for (const item of brief.watch_items || []) {
    const row = reportNode("div", "report-watch-item");
    row.append(reportNode("p", "report-impact-status", item.status === "action_required" ? "需要推进" : "持续关注"));
    row.append(reportNode("strong", "", item.title), reportNode("p", "", item.question));
    row.append(reportNode("small", "", [item.owner ? `负责人 ${item.owner}` : "负责人未指定", item.due_on ? `核对日期 ${item.due_on}` : "日期未指定", `审核人 ${item.reviewed_by || "未记录"}`].join(" · ")));
    const source = reportNode("button", "report-source text-button", `${item.source_reference} · ${item.source_locator} · 观察版本 ${item.observation_version} · SHA ${item.source_sha256.slice(0, 12)} ↗`);
    source.type = "button";
    source.addEventListener("click", () => openSource(state.sources.find((row) => row.id === item.source_id && row.content_sha256 === item.source_sha256), item.source_locator));
    row.append(source); watchSection.append(row);
  }
  if (brief.watch_items_truncated) watchSection.append(reportNode("p", "report-caveat", "仅展示前 20 项；完整清单见动态监控页。"));
  root.append(watchSection);
  if (brief.resolved_watch_items?.length) {
    const resolvedSection = reportSection("本期已处理事项");
    resolvedSection.append(reportNode("p", "report-caveat", "已处理只表示团队结束当前跟进，不代表相关风险已经消失。"));
    for (const item of brief.resolved_watch_items) {
      const row = reportNode("div", "report-watch-item");
      row.append(reportNode("strong", "", item.title), reportNode("p", "", item.question));
      if (item.resolution_reason) row.append(reportNode("p", "", `处理说明：${item.resolution_reason}`));
      row.append(reportNode("small", "", `处理于 ${dateText(item.resolved_at)} · 审核人 ${item.reviewed_by || "未记录"}`));
      const source = reportNode("button", "report-source text-button", `${item.source_reference} · ${item.source_locator} · SHA ${item.source_sha256.slice(0, 12)} ↗`);
      source.type = "button";
      source.addEventListener("click", () => openSource(state.sources.find((entry) => entry.id === item.source_id && entry.content_sha256 === item.source_sha256), item.source_locator));
      row.append(source); resolvedSection.append(row);
    }
    if (brief.resolved_watch_items_truncated) resolvedSection.append(reportNode("p", "report-caveat", "仅展示前 20 项；完整历史见动态监控页。"));
    root.append(resolvedSection);
  }
  const coverage = reportSection("监控范围");
  const domainList = reportNode("div", "report-domains");
  const statusText = { evidence_available: "有可核对记录", needs_review: "待核实", missing: "缺资料", not_run: "未运行", no_results: "本轮无结果", source_failure: "来源故障" };
  for (const domain of brief.domains) {
    const row = reportNode("div", "report-domain");
    row.append(reportNode("strong", "", domain.title), reportNode("span", "", statusText[domain.status] || domain.status));
    domainList.append(row);
  }
  coverage.append(domainList); root.append(coverage);
  const trends = reportSection("跨期指标");
  trends.append(reportNode("p", "report-caveat", "只并列同口径的已确认指标；年度合并财务实际值仅在相邻两期且无冲突时显示变动，计算值不等同于企业已披露结论。"));
  if (!brief.metric_trends?.length) trends.append(reportNode("p", "report-empty", "暂无可比较的已确认指标。"));
  for (const series of brief.metric_trends || []) {
    const group = reportNode("div", "report-trend");
    group.append(reportNode("h4", "", `${series.name} · ${series.unit} · ${series.basis === "actual" ? "实际" : series.basis === "forecast" ? "预测" : series.basis === "budget" ? "预算" : "口径未判定"}`));
    if (series.scope) group.append(reportNode("p", "report-trend-scope", `${series.scope} · ${series.cadence === "year" ? "年度" : series.cadence === "quarter" ? "季度" : "月度"}`));
    if (series.latest_change?.status === "comparable") {
      group.append(reportNode("p", "report-trend-change", `${series.latest_change.period} 较 ${series.latest_change.previous_period}：${signedValue(series.latest_change.absolute)} ${series.unit} · ${signedValue(series.latest_change.percent)}%（由下方两期记录计算）`));
    }
    for (const point of series.points) {
      const row = reportNode("div", "report-trend-row");
      row.append(reportNode("span", "", point.period));
      row.append(reportNode("strong", "", point.value));
      const source = reportNode("button", "report-source text-button", `${point.source_locator} · SHA ${point.source_sha256.slice(0, 12)}${point.same_period_conflict ? " · 同期多值待核对" : ""} ↗`);
      source.type = "button";
      source.addEventListener("click", () => openSource(state.sources.find((item) => item.id === point.source_id), point.source_locator));
      row.append(source, reportNode("small", "report-review-proof", `观察版本 ${point.observation_version} · ${locatorStatusText(point.locator_status)} · ${reviewProofText(point.review_proof)}`)); group.append(row);
    }
    if (series.truncated_points) group.append(reportNode("p", "report-caveat", "只展示最近 12 个数据点。"));
    trends.append(group);
  }
  if (brief.metric_trends_truncated) trends.append(reportNode("p", "report-caveat", "只展示最近 6 组指标；完整序列见动态监控页。"));
  root.append(trends);
  const evidence = reportSection("已确认记录与出处");
  if (!brief.confirmed_facts.length) evidence.append(reportNode("p", "report-empty", "暂无经过人工确认且登记了来源定位的观察。"));
  for (const fact of brief.confirmed_facts) {
    const row = reportNode("div", "report-fact");
    row.append(reportNode("strong", "", fact.statement));
    const value = [fact.value, fact.unit, fact.period, fact.basis === "actual" ? "实际" : fact.basis === "forecast" ? "预测" : fact.basis === "budget" ? "预算" : "口径未判定"].filter(Boolean).join(" · ");
    row.append(reportNode("p", "", value));
    const source = reportNode("button", "report-source text-button", `${fact.source_reference} · ${fact.source_locator} ↗`);
    source.type = "button";
    source.addEventListener("click", () => openSource(state.sources.find((item) => item.id === fact.source_id), fact.source_locator));
    row.append(source, reportNode("small", "report-version", `观察版本 ${fact.observation_version ?? "待同步"} · 原件 SHA ${fact.source_sha256.slice(0, 12)} · ${locatorStatusText(fact.locator_status)}`), reportNode("small", "report-review-proof", reviewProofText(fact.review_proof))); evidence.append(row);
  }
  if (brief.confirmed_facts_truncated) evidence.append(reportNode("p", "report-caveat", "只展示最近 30 条；完整记录见事实核对页。"));
  root.append(evidence);
  const gaps = reportSection("待核实与缺口");
  if (!brief.gaps.length && !brief.pending_review_count && !brief.candidate_count) gaps.append(reportNode("p", "report-empty", "当前六域已有记录；仍需核查证据质量与最新时点。"));
  for (const gap of brief.gaps) {
    const row = reportNode("p", "report-gap"); row.append(reportNode("strong", "", `${gap.domain} · ${statusText[gap.status] || gap.status}`), reportNode("span", "", gap.note)); gaps.append(row);
  }
  if (brief.candidate_count || brief.pending_review_count) gaps.append(reportNode("p", "report-caveat", `${brief.candidate_count} 条候选待人工核对；${brief.pending_review_count} 条旧判断因新材料待回看。`));
  root.append(gaps);
  const external = reportSection("公开线索");
  if (!brief.external_leads.length) external.append(reportNode("p", "report-empty", "暂无已收录的公开候选线索；请结合来源运行状态判断，不能据此认定没有动态。"));
  for (const leadItem of brief.external_leads) external.append(reportNode("p", "report-lead", `${leadItem.title} · 收录于 ${dateText(leadItem.first_seen_at)} · 未核验`));
  if (brief.external_leads_truncated) external.append(reportNode("p", "report-caveat", "只展示最近 10 条；完整线索见动态监控页。"));
  root.append(external);
  const sourceChecks = reportSection("公开来源检查");
  if (!brief.source_checks?.length) sourceChecks.append(reportNode("p", "report-empty", "尚未配置公开来源；不能据此认定项目没有外部动态。"));
  for (const check of brief.source_checks || []) {
    const row = reportNode("div", "report-domain");
    const status = monitoringStatus({ last_run_status: check.status, last_run_error_code: check.error_code, last_matched_count: check.matched_count });
    const count = check.item_count === null ? "数量未记录" : `返回 ${check.item_count} 条`;
    row.append(reportNode("strong", "", check.provider), reportNode("span", "", `${check.enabled ? status : `已暂停 · ${status}`} · ${count}`));
    sourceChecks.append(row);
  }
  if (brief.source_checks_truncated) sourceChecks.append(reportNode("p", "report-caveat", "只展示前 20 个来源；完整清单见动态监控页。"));
  sourceChecks.append(reportNode("p", "report-caveat", "显示各来源已记录的最近状态；来源故障或限流不等于没有新闻，名称匹配也不等于主体已核验。"));
  root.append(sourceChecks, reportNode("footer", "report-foot", `${brief.disclaimer} 本页由规则投影，不含模型生成的投资判断。来源版本与人工审核记录以系统原始记录为准。`));
}
async function loadReport() {
  const projectId = state.current?.id;
  if (!projectId) return;
  const root = $("report-content"); root.replaceChildren(reportNode("p", "muted", "正在整理当前快照…"));
  try {
    const brief = await request(`/api/projects/${projectId}/report/current`);
    if (state.current?.id === projectId && state.view === "report") renderReport(brief);
  } catch (error) {
    if (state.current?.id === projectId) root.replaceChildren(reportNode("p", "state-note error", `简报暂不可用：${error.message}`));
  }
}
async function refreshProject() {
  if (!state.current) return;
  const id = state.current.id;
  const revision = state.projectRevision;
  try {
    const reading = state.teamMode ? Promise.resolve({data:null,error:null}) : request(`/api/workspace/projects/${id}/reading`).then(data=>({data,error:null}),error=>({data:null,error}));
    const profile = state.teamMode ? Promise.resolve({data:null,error:null}) : request(`/api/workspace/projects/${id}/profile`).then(data=>({data,error:null}),error=>({data:null,error}));
    const external = state.teamMode ? Promise.resolve({data:null,error:null}) : request(`/api/projects/${id}/monitoring/external`).then(data=>({data,error:null}),error=>({data:null,error}));
    const externalAnalysis = state.teamMode ? Promise.resolve({data:null,error:null}) : request(`/api/workspace/projects/${id}/external-analysis`).then(data=>({data,error:null}),error=>({data:null,error}));
    const briefing = state.teamMode ? Promise.resolve({data:null,error:null}) : request(`/api/workspace/projects/${id}/briefing`).then(data=>({data,error:null}),error=>({data:null,error}));
    const core = Promise.all([
      request(`/api/projects/${id}/summary`), request(`/api/projects/${id}/sources`),
      request(`/api/projects/${id}/intake`), request(`/api/projects/${id}/observations`),
      request(`/api/projects/${id}/review-impacts`),
      request(`/api/projects/${id}/capabilities`),
    ]);
    const monitoring = Promise.all([
      request(`/api/projects/${id}/monitoring/plans`),
      request(`/api/projects/${id}/monitoring/jobs`),
      request(`/api/projects/${id}/monitoring/signals`),
    ]).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const metrics = request(`/api/projects/${id}/metric-series`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const financial = request(`/api/projects/${id}/financial-screen`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const domains = request(`/api/projects/${id}/monitoring/domains`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const competition = request(`/api/projects/${id}/competitive-position`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const watchItems = request(`/api/projects/${id}/watch-items`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const holdings = request(`/api/projects/${id}/holding-snapshots`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const watchChanges = request(`/api/projects/${id}/watch-item-changes`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const tasks = request(`/api/projects/${id}/tasks`).then((data) => ({ data, error: null }), (error) => ({ data: null, error }));
    const [[summary, sources, jobs, observations, impacts, capabilities], monitoringResult, metricsResult, financialResult, domainsResult, competitionResult, watchResult, changeResult, tasksResult, holdingResult, readingResult, profileResult, externalResult, briefingResult, analysisResult] = await Promise.all([core, monitoring, metrics, financial, domains, competition, watchItems, watchChanges, tasks, holdings, reading, profile, external, briefing, externalAnalysis]);
    if (state.current?.id !== id || state.projectRevision !== revision) return;
    state.current = summary.project; state.sources = sources; state.jobs = jobs; state.observations = observations; state.impacts = impacts;
    state.readingError = readingResult.error;
    state.external=externalResult.data;state.externalError=externalResult.error;
    state.externalAnalysis=analysisResult.data;state.externalAnalysisError=analysisResult.error;
    state.briefing=briefingResult.data;state.briefingError=briefingResult.error;
    if (readingResult.data) state.reading = readingResult.data;
    state.profileError = profileResult.error;
    if (profileResult.error) state.profile = null;
    if (profileResult.data && (!state.profile || profileResult.data.version >= state.profile.version)) state.profile = profileResult.data;
    state.capabilities = capabilities; applyCapabilities();
    state.metricError = metricsResult.error; state.metricSeries = metricsResult.data;
    state.financialError = financialResult.error; state.financialScreen = financialResult.data;
    state.domainsError = domainsResult.error; state.monitoringDomains = domainsResult.data;
    state.competitionError = competitionResult.error; state.competition = competitionResult.data;
    state.watchError = watchResult.error || changeResult.error; state.watchItems = watchResult.data || []; state.watchChanges = changeResult.data || [];
    state.holdingSnapshots = holdingResult.data || [];
    state.monitoringError = monitoringResult.error;
    if (monitoringResult.data) [state.monitoringPlans, state.monitoringJobs, state.signals] = monitoringResult.data;
    else { state.monitoringPlans = []; state.monitoringJobs = []; state.signals = []; }
    state.coverageError = Boolean(tasksResult.error);
    state.coverageTask = tasksResult.data?.find((item) => item.kind === "coverage") || null;
    renderProjects(); renderProject(); schedulePoll();
  } catch (error) {
    if (state.current?.id !== id || state.projectRevision !== revision) return;
    if (error.status === 404 && state.teamMode) {
      state.current = null; state.capabilities = { read: false, propose: false, review: false, manage: false };
      state.metricSeries = null; state.metricError = null; state.metricDraft = null;
      state.financialScreen = null; state.financialError = null;
      state.monitoringDomains = null; state.domainsError = null;
      state.competition = null; state.competitionError = null;
      state.watchItems = []; state.watchChanges = []; state.watchError = null; state.watchReview = null; state.watchChange = null; state.watchChangeReview = null;
      state.holdingSnapshots = []; state.holdingDraft = null; state.holdingReview = null;
      state.selectedSource = null; state.selectedCandidates.clear(); state.batchDecisions.clear(); $("reader").hidden = true;
      if ($("batch-review-dialog").open) $("batch-review-dialog").close();
      clearTimeout(state.timer); sessionStorage.removeItem("project-watch-current");
      await loadProjects(false); applyCapabilities();
      toast("当前项目访问已变化，请重新选择。");
    } else toast(error.message);
  }
}
function jobFor(source) { return state.jobs.find((job) => job.source_id === source.id); }
function statusText(job) {
  if (!job) return "仅登记来源";
  return ({ queued: "等待解析", processing: "正在解析", completed: "已提取文本", needs_ocr: "含待识别页面 · 可查看处理结果", failed: "解析失败" })[job.status] || job.status;
}
function renderProject() {
  const project = state.current;
  $("project-name").textContent = project.name;
  $("project-number").textContent = "项目监控 · 经营情况与近期变化";
  $("identity-status").textContent = project.identity_status === "verified" ? "主体已核验" : "主体未核验";
  $("project-created").textContent = `建立于 ${dateText(project.created_at)} · ${state.teamMode ? "团队模式 Alpha" : privateLocalIntake ? "本机私有试用" : "本机开发预览"}`;
  const pending = state.jobs.filter((job) => job.status === "queued" || job.status === "processing").length;
  const candidates = state.observations.filter((item) => item.status === "candidate");
  const confirmed = state.observations.filter((item) => item.status === "confirmed");
  const rejected = state.observations.filter((item) => item.status === "rejected");
  const pendingImpacts = state.impacts.filter((item) => item.status === "pending");
  $("source-count").textContent = String(state.sources.length);
  $("pending-count").textContent = String(pending);
  $("candidate-count").textContent = String(candidates.length);
  $("confirmed-count").textContent = String(confirmed.length);
  $("material-tab-count").textContent = state.sources.length ? String(state.sources.length) : "";
  $("review-tab-count").textContent = candidates.length + pendingImpacts.length ? String(candidates.length + pendingImpacts.length) : "";
  const visibleNews = state.teamMode ? state.signals.length : readingNews(state.external, project.id).length;
  $("monitor-tab-count").textContent = visibleNews ? String(visibleNews) : "";
  $("next-action").textContent = !state.sources.length ? (state.capabilities.propose ? "尚无材料。上传 BP 或项目文件，建立第一份可追溯的来源。" : "尚无材料，等待有权限的同事添加。") : pending ? `${pending} 份材料正在后台处理。你可以继续阅读已完成的来源。` : pendingImpacts.length ? `${pendingImpacts.length} 条旧判断涉及新材料，待人工回看；原判断尚未改变。` : candidates.length ? `${candidates.length} 条候选摘录等待核对，${state.capabilities.review ? "核对后可确认" : "由审核人确认"}。` : "材料已登记。没有匹配到可核对的短摘录；可以阅读原件或手动提出观察。";
  renderMaterials(); renderMonitoring(); renderObservations(candidates, confirmed, rejected);
  renderCoverage();
  client?.refreshProject();
}
function renderCoverage() {
  const box = $("coverage-result"); box.replaceChildren();
  if (state.coverageError) { box.textContent = "研究覆盖暂不可用，请稍后重试。"; return; }
  if (!state.coverageTask) { box.textContent = "尚未检查研究覆盖。"; return; }
  const outcome = state.coverageTask.outcome;
  const line = document.createElement("p");
  line.textContent = `${outcome.source_count} 份材料 · ${outcome.covered_topics.length} 个方向有已登记观察 · ${outcome.missing_topics.length} 个方向待补`;
  box.append(line);
  const currentIds = new Set(state.sources.map((source) => source.id));
  if (state.coverageTask.source_ids.length !== currentIds.size || state.coverageTask.source_ids.some((id) => !currentIds.has(id))) {
    const stale = document.createElement("p"); stale.textContent = "材料清单已变化，请重新检查覆盖。"; box.append(stale);
  }
  if (outcome.missing_topics.length) {
    const list = document.createElement("ul");
    for (const topic of outcome.missing_topics) { const item = document.createElement("li"); item.textContent = topic; list.append(item); }
    box.append(list);
  }
}
function monitoringStatus(plan, job) {
  if (job?.status === "queued") return "等待检查";
  if (job?.status === "processing") return "正在检查";
  if (job?.status === "failed") return `任务失败 · ${job.error_code || "可重试"}`;
  if (plan.last_run_error_code === "quota_exhausted") return "本月额度用尽 · 已停止外发";
  if (plan.last_run_error_code === "credential_missing" || plan.last_run_error_code === "credential_rejected") return "API Key 不可用 · 联系管理员";
  if (plan.last_run_status === "rate_limited" || (plan.last_run_status === "unreachable" && plan.last_run_error_code === "rate_limited")) return "来源限流 · 请稍后再试";
  if (plan.last_run_status === "unreachable" && plan.last_run_error_code === "connection_failed") return "连接失败 · 请检查网络或代理";
  if (plan.last_run_status === "completed") {
    if (plan.last_matched_count === null) return "旧回执 · 未按名称筛选";
    return plan.last_matched_count === 0 ? "来源可达 · 未匹配项目" : "检查完成";
  }
  return ({ not_run: "尚未检查", no_results: "来源可达 · 无结果", unreachable: "来源不可达" })[plan.last_run_status] || "状态未知";
}
function cadenceText(minutes) { return minutes === 60 ? "每小时" : minutes === 1440 ? "每天" : minutes === 10080 ? "每周" : `每 ${minutes} 分钟`; }
function targetHost(target) { try { return new URL(target).hostname; } catch { return "来源地址异常"; } }
function renderLocalMaterialUpdates() {
  const box = $("local-material-updates"); box.replaceChildren();
  const documents = state.sources.filter((source) => source.kind === "document");
  const pending = state.impacts.filter((impact) => impact.status === "pending");
  $("local-material-status").textContent = documents.length
    ? `${documents.length} 份材料 · ${pending.length} 条旧判断待回看` : "尚无材料";
  if (!documents.length) {
    empty(box, state.capabilities.propose ? "还没有材料。拖入 BP 或其他项目文件，解析会在后台开始。" : "还没有材料，等待有权限的同事添加。");
    return;
  }
  const observations = new Map(state.observations.map((item) => [item.id, item]));
  for (const source of documents.slice(-8).reverse()) {
    const impacts = pending.filter((impact) => impact.trigger_source_id === source.id);
    const row = document.createElement("article"); row.className = "local-material-entry";
    const open = document.createElement("button"); open.type = "button"; open.className = "local-material-open";
    const title = document.createElement("strong"); title.textContent = source.reference;
    const meta = document.createElement("span"); meta.textContent = `${source.topic && source.topic !== "未分类" ? source.topic : "待整理"} · ${statusText(jobFor(source))} · ${dateText(source.created_at)}`;
    open.append(title, meta); open.addEventListener("click", () => openSource(source)); row.append(open);
    if (impacts.length) {
      const review = document.createElement("div"); review.className = "local-material-review";
      for (const impact of impacts.slice(0, 2)) {
        const button = document.createElement("button"); button.type = "button";
        button.textContent = `旧判断待回看 · ${observations.get(impact.observation_id)?.statement || impact.topic}`;
        button.addEventListener("click", () => state.capabilities.review ? openImpactReview(impact) : openSource(source));
        review.append(button);
      }
      if (impacts.length > 2) {
        const more = document.createElement("button"); more.type = "button"; more.textContent = `另有 ${impacts.length - 2} 条 · 查看事实核对 →`;
        more.addEventListener("click", () => { state.view = "review"; renderView(); }); review.append(more);
      }
      row.append(review);
    }
    box.append(row);
  }
}
function renderMetricSeries() {
  const box = $("metric-series-list"); box.replaceChildren();
  const note = $("metric-unsequenced"); note.hidden = true;
  if (state.metricError || !state.metricSeries) {
    $("metric-series-status").textContent = "暂不可用";
    empty(box, "指标记录读取失败；材料与事实核对仍可使用。");
    return;
  }
  const result = state.metricSeries;
  $("metric-series-status").textContent = result.total_series ? `${result.total_series} 组 · 仅已确认` : "尚无已确认指标";
  if (!result.series.length) empty(box, "还没有核对过的经营或财务数字。可先在资料中查看报表并核对数值。");
  for (const series of result.series) {
    const section = document.createElement("section"); section.className = "metric-series";
    const heading = document.createElement("div"); heading.className = "metric-series-heading";
    const title = document.createElement("h4"); title.textContent = series.name;
    const cadence = { year: "年度", quarter: "季度", month: "月度" }[series.cadence] || series.cadence;
    const basis = { actual: "实际", forecast: "预测", budget: "预算", unknown: "未判定" }[series.basis] || "未判定";
    const meta = document.createElement("span"); meta.textContent = `${series.subject} · ${basis} · ${series.scope || "未指定口径"} · ${cadence}`;
    heading.append(title, meta); section.append(heading);
    if (series.latest_change?.status === "comparable") {
      const change = document.createElement("div"); change.className = "metric-series-change";
      const percent = document.createElement("strong"); percent.textContent = `${signedValue(series.latest_change.percent)}%`;
      const context = document.createElement("span"); context.textContent = `${series.latest_change.period} 较 ${series.latest_change.previous_period} · ${signedValue(series.latest_change.absolute)} ${series.unit} · 由下方两期记录计算`;
      change.append(percent, context); section.append(change);
    }
    const points = document.createElement("div"); points.className = "metric-points";
    for (const point of series.points.slice(-4).reverse()) {
      const source = state.sources.find((item) => item.id === point.source_id);
      const button = document.createElement("button"); button.type = "button"; button.className = "metric-point";
      const period = document.createElement("span"); period.textContent = point.period;
      const value = document.createElement("strong"); value.textContent = `${point.number} ${series.unit}`;
      const evidence = document.createElement("small"); evidence.textContent = `${point.same_period_conflict ? "同期待核对 · " : ""}${point.source_reference} · ${point.source_locator}`;
      button.append(period, value, evidence);
      button.addEventListener("click", () => openSource(source, point.source_locator)); points.append(button);
    }
    section.append(points);
    const foot = document.createElement("p"); foot.className = "metric-series-foot";
    const shown = Math.min(series.points.length, 4);
    const changeReasons = { missing_previous: "缺少相邻上一年度；不计算变化。", same_period_conflict: "同期存在多值；请先核对原件。", nonpositive_baseline: "基期为零或负值；不计算百分比。", source_unavailable: "来源定位不完整；不计算变化。", uncomputable: "数值超出安全计算范围；不计算变化。" };
    foot.textContent = changeReasons[series.latest_change?.status] || (series.total_points === 1
      ? "等待下一期；不作变化判断。"
      : `共 ${series.total_points} 条已确认记录${series.total_points > shown ? ` · 显示最近 ${shown} 条` : ""}`);
    section.append(foot); box.append(section);
  }
  if (result.unsequenced_count || result.truncated) {
    note.hidden = false;
    note.textContent = `${result.unsequenced_count ? `${result.unsequenced_count} 条旧数值记录的期间或口径不适合入序列。` : ""}${result.truncated ? "仅展示最近六组指标。" : ""}完整记录仍在“事实核对”。`;
  }
}
function renderFinancialScreen() {
  const box = $("financial-screen-list"); box.replaceChildren();
  const status = $("financial-screen-status");
  if (state.financialError || !state.financialScreen) {
    status.textContent = "暂不可用";
    empty(box, "财务核验读取失败；原件和指标记录仍可查看。");
    return;
  }
  const result = state.financialScreen;
  const analysis = result.analysis;
  const count = result.accepted_count || 0;
  status.textContent = count ? `${count} 条已确认数值 · ${analysis.as_of_period || "期间待定"}` : "等待已确认数据";
  if (!count) {
    empty(box, "暂时不能进行年度财务比较。需要先核对同一公司的年度合并报表实际数。");
  } else {
    const summary = document.createElement("div"); summary.className = "financial-summary";
    const caption = document.createElement("div");
    const heading = document.createElement("strong"); heading.textContent = analysis.status === "screened" ? "跨期核验" : "单期核验";
    const detail = document.createElement("span"); detail.textContent = `${analysis.periods.join("、")} · ${analysis.metric_count} 项指标 · 统一折算为万元`;
    caption.append(heading, detail); summary.append(caption);
    for (const [value, label] of [[analysis.summary?.warning_count || 0, "复核线索"], [analysis.summary?.data_gap_count || 0, "证据缺口"], [analysis.summary?.verified_count || 0, "勾稽通过"]]) {
      const cell = document.createElement("div"); cell.className = "financial-stat";
      const number = document.createElement("strong"); number.textContent = String(value);
      const text = document.createElement("span"); text.textContent = label;
      cell.append(number, text); summary.append(cell);
    }
    box.append(summary);
    const relevant = (analysis.checks || []).filter((check) => check.status === "warning" || check.status === "gap");
    const checks = document.createElement("div"); checks.className = "financial-checks";
    if (!relevant.length) {
      const clear = document.createElement("p"); clear.className = "financial-clear";
      clear.textContent = "当前可计算的勾稽项未出现异常；仍需留意未取得的资料。"; checks.append(clear);
    }
    for (const check of relevant.slice(0, 6)) {
      const row = document.createElement("article"); row.className = `financial-check financial-${check.status}`;
      const title = document.createElement("strong"); title.textContent = check.title;
      const tag = document.createElement("span"); tag.textContent = check.status === "warning" ? "待复核" : "待补资料";
      const note = document.createElement("p"); note.textContent = check.observation || check.evidence_needed?.join("、") || "";
      row.append(title, tag, note); checks.append(row);
    }
    if (relevant.length > 6) {
      const more = document.createElement("p"); more.className = "financial-more";
      more.textContent = `另有 ${relevant.length - 6} 项尚未展示。`; checks.append(more);
    }
    box.append(checks);
  }
  const evidence = (result.evidence || []).slice(0, 6);
  if (evidence.length) {
    const sources = document.createElement("div"); sources.className = "financial-evidence";
    const title = document.createElement("strong"); title.textContent = "本次采用的来源"; sources.append(title);
    for (const item of evidence) {
      const source = state.sources.find((entry) => entry.id === item.source_id);
      const button = document.createElement("button"); button.type = "button";
      button.textContent = `${item.period} · ${item.metric} · ${source?.reference || "来源已移除"} · ${item.source_locator}`;
      button.disabled = !source;
      button.addEventListener("click", () => openSource(source, item.source_locator)); sources.append(button);
    }
    box.append(sources);
  }
  const excluded = Object.values(result.excluded || {}).reduce((total, value) => total + value, 0);
  if (excluded) {
    const note = document.createElement("p"); note.className = "financial-excluded";
    note.textContent = `${excluded} 条记录未进入本次核验（如预测、口径不符或缺少定位）。可在“事实核对”查看原记录。`;
    box.append(note);
  }
}
function renderCompetition() {
  const peers = $("competition-peers"), comparison = $("competition-comparison"), status = $("competition-status");
  peers.replaceChildren(); comparison.replaceChildren();
  $("competition-form").hidden = !state.capabilities.propose;
  const selector = $("competition-link");
  const selected = selector.value;
  selector.replaceChildren(new Option("仅登记名称", ""), ...state.projects.filter((project) => project.id !== state.current?.id).map((project) => new Option(project.name, project.id)));
  if ([...selector.options].some((option) => option.value === selected)) selector.value = selected;
  if (state.competitionError || !state.competition) {
    status.textContent = "暂不可用"; empty(peers, "同行集暂不可用；不会据此生成竞争排名。"); return;
  }
  const result = state.competition;
  const approved = result.peers.filter((peer) => peer.status === "confirmed");
  status.textContent = `${approved.length} 家已确认${result.peers.some((peer) => peer.status === "candidate") ? " · 有候选待核对" : ""}`;
  if (!result.peers.length) empty(peers, "还没有登记竞争对象。可以先填公司名，数据以后再补。");
  for (const peer of result.peers) {
    const row = document.createElement("article"); row.className = "competition-peer";
    const info = document.createElement("div");
    const name = document.createElement("strong"); name.textContent = peer.name;
    const note = document.createElement("small");
    note.textContent = peer.status === "candidate" ? "候选 · 尚未进入比较" : peer.status === "rejected" ? "已排除" : result.peer_access[peer.id] === "readable" ? "已确认 · 可读取关联项目" : result.peer_access[peer.id] === "unavailable" ? "已确认 · 关联项目当前不可读" : "已确认 · 尚无关联项目";
    info.append(name, note); row.append(info);
    if (peer.status === "candidate" && state.capabilities.review) {
      const actions = document.createElement("div"); actions.className = "competition-actions";
      for (const [action, label] of [["confirm", "确认同行"], ["reject", "排除"]]) {
        const button = document.createElement("button"); button.type = "button"; button.className = "text-button"; button.textContent = label;
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await request(`/api/projects/${state.current.id}/competitive-peers/${peer.id}/review`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action, expected_version: peer.version, reviewer: state.teamMode ? null : "本机预览用户" }),
            });
            await refreshProject(); toast(state.teamMode ? "同行集已更新" : "本机预览已记录选择；正式团队确认须登录");
          } catch (error) { toast(error.message); button.disabled = false; }
        }); actions.append(button);
      }
      row.append(actions);
    }
    peers.append(row);
  }
  const summary = result.comparison;
  if (summary.status === "missing_peer_set") { empty(comparison, "尚无法比较：请先确认至少一家竞争对象。"); return; }
  if (!summary.comparisons.length) {
    empty(comparison, "尚无法比较：缺少同期间、同单位、同范围、同口径的已确认数字及来源定位。");
  } else {
    const heading = document.createElement("h4"); heading.textContent = "部分同口径记录（不排名）"; comparison.append(heading);
    for (const item of summary.comparisons.slice(0, 6)) {
      const line = document.createElement("article"); line.className = "competition-comparison-line";
      const metric = document.createElement("strong"); metric.textContent = `${item.name} · ${item.period} · ${item.scope} · ${item.basis === "actual" ? "实际" : item.basis === "forecast" ? "预测" : "预算"}`;
      const own = document.createElement("p"); own.textContent = `本项目 ${item.subject.value} ${item.unit} · 来源 ${item.subject.source_id.slice(0, 8)} / ${item.subject.source_locator}`;
      line.append(metric, own);
      for (const value of item.peers) {
        const peer = result.peers.find((entry) => entry.id === value.entity_id);
        const detail = document.createElement("p"); detail.textContent = `${peer?.name || "同行"} ${value.value} ${item.unit} · 来源 ${value.source_id.slice(0, 8)} / ${value.source_locator}`;
        line.append(detail);
      }
      comparison.append(line);
    }
  }
  if (summary.gaps.length) {
    const gap = document.createElement("small"); gap.className = "competition-gap";
    gap.textContent = `另有 ${summary.gaps.length} 处不可比或待补证据；不同期间、口径或缺少原件定位均不计入比较。`;
    comparison.append(gap);
  }
}
function renderMonitoringDomains() {
  const box = $("monitoring-domains"); box.replaceChildren();
  const status = $("domain-monitor-status");
  if (state.domainsError || !state.monitoringDomains) {
    status.textContent = "暂不可用";
    empty(box, "监控范围读取失败；其他项目内容仍可使用。");
    return;
  }
  const result = state.monitoringDomains;
  status.textContent = `${result.evidence_available_count} / ${result.total_count} 个方向已有记录或线索`;
  const labels = { evidence_available: "有记录/线索", needs_review: "待核对", missing: "缺资料", not_run: "未运行", no_results: "本轮无结果", source_failure: "来源故障" };
  for (const item of result.domains) {
    const row = document.createElement("article"); row.className = "monitoring-domain";
    const top = document.createElement("div"); top.className = "monitoring-domain-head";
    const name = document.createElement("strong"); name.textContent = item.title;
    const badge = document.createElement("span"); badge.className = `domain-status domain-${item.status}`;
    badge.textContent = labels[item.status] || "待核对";
    top.append(name, badge);
    const detail = document.createElement("p"); detail.textContent = item.note;
    const meta = document.createElement("small");
    meta.textContent = `${item.evidence_count} 条已采用记录或匹配线索${item.pending_count ? ` · ${item.pending_count} 条待核对` : ""}${item.source_count ? ` · ${item.source_count} 个关联来源` : ""}${item.last_recorded_at ? ` · 最近登记 ${dateText(item.last_recorded_at)}` : ""}${item.last_checked_at ? ` · 最近检查 ${dateText(item.last_checked_at)}` : ""}${item.source_failure_count ? ` · ${item.source_failure_count} 个来源故障` : ""}`;
    row.append(top, detail, meta);
    if (item.key === "capital") {
      const evidenceList = document.createElement("div"); evidenceList.className = "capital-evidence";
      for (const position of (item.holdings?.positions || [])) {
        const source = state.sources.find((entry) => entry.id === position.source_id && entry.content_sha256 === position.source_sha256);
        const button = document.createElement("button"); button.type = "button"; button.className = "capital-evidence-link";
        button.textContent = `${position.fund_name} · ${position.as_of} · 成本 ${position.invested_cost_wan ?? "未登记"} 万元 · 持股 ${position.ownership_pct ?? "未登记"}% · 价值 ${position.current_value_wan ?? "未登记"} 万元${position.moc ? ` · MOC ${position.moc}x` : ""}`;
        button.title = source ? `${source.reference} · ${position.source_locator} · 审核 ${position.reviewed_by}` : "来源版本不可用";
        button.disabled = !source;
        button.addEventListener("click", () => openSource(source, position.source_locator));
        evidenceList.append(button);
        if (position.new_material_count_since_review) {
          const notice = document.createElement("button"); notice.type = "button"; notice.className = "capital-evidence-link";
          notice.textContent = `${position.fund_name}：审核后新增 ${position.new_material_count_since_review} 份项目材料，尚未核对是否影响持仓 · 查看材料 →`;
          notice.addEventListener("click", () => { state.view = "materials"; renderView(); });
          evidenceList.append(notice);
        }
      }
      for (const conflict of (item.holdings?.conflicts || [])) {
        const warning = document.createElement("small");
        warning.textContent = `${conflict.fund_name} · ${conflict.as_of} 有 ${conflict.snapshot_ids.length} 条冲突快照，暂不展示持仓。`;
        evidenceList.append(warning);
      }
      for (const candidate of state.holdingSnapshots.filter((row) => row.status === "candidate")) {
        const button = document.createElement("button"); button.type = "button"; button.className = "capital-evidence-link";
        button.textContent = `${candidate.fund_name} · ${candidate.as_of} · 待审核`;
        button.addEventListener("click", () => state.capabilities.review ? openHoldingReview(candidate) : openSource(state.sources.find((source) => source.id === candidate.source_id), candidate.source_locator));
        evidenceList.append(button);
      }
      for (const evidence of (item.evidence || []).slice(0, 3)) {
        const source = state.sources.find((entry) => entry.id === evidence.source_id && entry.content_sha256 === evidence.source_sha256);
        const button = document.createElement("button"); button.type = "button"; button.className = "capital-evidence-link";
        button.textContent = `${evidence.metric} · ${evidence.value} ${evidence.unit} · ${evidence.period}`;
        button.title = source ? `${source.reference} · ${evidence.source_locator}` : "来源版本不可用";
        button.disabled = !source;
        button.addEventListener("click", () => openSource(source, evidence.source_locator));
        evidenceList.append(button);
      }
      if ((item.evidence || []).length > 3) {
        const more = document.createElement("small"); more.textContent = `另有 ${item.evidence.length - 3} 条，可在事实核对查看。`; evidenceList.append(more);
      }
      row.append(evidenceList);
    }
    box.append(row);
  }
}
function renderOperatingMetrics() {
  const box = $("operating-metrics-list"), status = $("operating-metrics-status");
  box.replaceChildren();
  const result = state.monitoringDomains?.operating_metrics;
  if (state.domainsError || !result) {
    status.textContent = "暂不可用";
    empty(box, "产能与产量记录暂不可用。");
    return;
  }
  status.textContent = `${result.actual_count} 条实际${result.planned_count ? ` · ${result.planned_count} 条预测/预算` : ""}${result.pending_count ? ` · ${result.pending_count} 条待核对` : ""}`;
  if (!result.evidence.length) { empty(box, result.note); return; }
  for (const entry of result.evidence) {
    const row = document.createElement("article"); row.className = "operating-metric";
    const heading = document.createElement("div"); heading.className = "operating-metric-heading";
    const name = document.createElement("strong"); name.textContent = entry.metric;
    const basis = document.createElement("span"); basis.textContent = { actual: "实际", forecast: "预测", budget: "预算" }[entry.basis] || "口径待核对";
    heading.append(name, basis);
    const value = document.createElement("p"); value.className = "operating-metric-value";
    const number = document.createElement("strong"); number.textContent = entry.value;
    const unit = document.createElement("span"); unit.textContent = entry.unit;
    value.append(number, unit);
    const context = document.createElement("small"); context.textContent = `${entry.scope} · ${entry.period}`;
    const source = state.sources.find((item) => item.id === entry.source_id && item.content_sha256 === entry.source_sha256);
    const link = document.createElement("button"); link.type = "button"; link.className = "activity-source";
    link.textContent = source ? "核对原件 ↗" : "来源版本不可用";
    link.title = source ? `${source.reference} · ${entry.source_locator}` : "来源版本不可用";
    link.disabled = !source;
    link.addEventListener("click", () => openSource(source, entry.source_locator));
    row.append(heading, value, context, link); box.append(row);
  }
  if (result.evidence_truncated || result.pending_count) {
    const note = document.createElement("p"); note.className = "activity-more";
    note.textContent = `${result.note}${result.evidence_truncated ? " 更多记录可在事实核对中查看。" : ""}`;
    box.append(note);
  }
}
function renderActivityTimeline() {
  const box = $("activity-timeline"); box.replaceChildren();
  const status = $("activity-status"), review = $("activity-review");
  const activity = state.monitoringDomains?.activity;
  if (state.domainsError || !activity) {
    status.textContent = "暂不可用"; review.hidden = true;
    empty(box, "经营、产品与风险记录暂不可用。");
    return;
  }
  status.textContent = `${activity.confirmed_count} 条已确认${activity.pending_count ? ` · ${activity.pending_count} 条待核对` : ""}`;
  review.hidden = !activity.pending_count;
  if (!activity.entries.length) { empty(box, "还没有核对过的业务记录。产品、客户和经营进展会在确认后显示于此。"); return; }
  for (const entry of activity.entries.slice(0, 6)) {
    const row = document.createElement("article"); row.className = "activity-entry";
    const when = document.createElement("div"); when.className = "activity-entry-when";
    const subject = document.createElement("strong"); subject.textContent = entry.subject;
    const recorded = document.createElement("small"); recorded.textContent = `登记于 ${dateText(entry.recorded_at)}`;
    when.append(subject, recorded);
    const body = document.createElement("div"); body.className = "activity-entry-body";
    const statement = document.createElement("p"); statement.textContent = entry.statement_excerpt;
    const context = document.createElement("small");
    const basis = { actual: "实际", forecast: "预测", budget: "预算", unknown: "口径未判定" }[entry.basis] || "口径未判定";
    context.textContent = [entry.period ? `资料期间 ${entry.period}` : "期间未登记", entry.scope ? `范围 ${entry.scope}` : null, entry.number !== null ? `${basis} ${entry.number} ${entry.unit || ""}`.trim() : null].filter(Boolean).join(" · ");
    body.append(statement, context);
    const source = state.sources.find((item) => item.id === entry.source_id && item.content_sha256 === entry.source_sha256);
    const link = document.createElement("button"); link.type = "button"; link.className = "activity-source";
    link.textContent = source ? "核对原件 ↗" : "来源版本不可用";
    link.title = source ? `${source.reference} · ${entry.source_locator}` : "来源版本不可用";
    link.disabled = !source;
    link.addEventListener("click", () => openSource(source, entry.source_locator));
    row.append(when, body, link); box.append(row);
  }
  if (activity.confirmed_count > 6) {
    const more = document.createElement("p"); more.className = "activity-more";
    more.textContent = `另有 ${activity.confirmed_count - 6} 条，完整记录在“事实核对”。`;
    box.append(more);
  }
}
function renderWatchItems() {
  const box = $("watch-items"), status = $("watch-status"); box.replaceChildren();
  if (state.watchError) { status.textContent = "读取失败"; empty(box, "关注事项暂不可用；已有观察与材料仍可查看。"); return; }
  const open = state.watchItems.filter((item) => ["watching", "action_required"].includes(item.status));
  const pending = state.watchItems.filter((item) => item.status === "candidate");
  const pendingChanges = state.watchChanges.filter((change) => change.status === "candidate");
  const resolved = state.watchItems.filter((item) => item.status === "resolved");
  status.textContent = `${open.length} 项在跟进${pending.length + pendingChanges.length ? ` · ${pending.length + pendingChanges.length} 项待审核` : ""}${resolved.length ? ` · ${resolved.length} 项已处理` : ""}`;
  const visible = [...state.watchItems].filter((item) => item.status !== "rejected").sort((a, b) => {
    const rank = { action_required: 0, watching: 1, candidate: 2, resolved: 3 };
    return (rank[a.status] ?? 3) - (rank[b.status] ?? 3) || (a.due_on || "9999").localeCompare(b.due_on || "9999");
  });
  if (!visible.length) { empty(box, "尚无关注事项。确认观察后，可把需要持续核对的问题留在这里。"); return; }
  for (const item of visible) {
    const row = document.createElement("article"); row.className = "watch-item";
    const heading = document.createElement("div"); heading.className = "watch-item-head";
    const title = document.createElement("strong"); title.textContent = item.title;
    const flag = document.createElement("span"); flag.textContent = ({ action_required: "需推进", watching: "持续关注", candidate: "待审核", resolved: "已处理" })[item.status] || item.status;
    heading.append(title, flag);
    const question = document.createElement("p"); question.textContent = item.question;
    const meta = document.createElement("small"); meta.textContent = [item.owner ? `负责人 ${item.owner}` : "负责人未指定", item.due_on ? `核对日期 ${item.due_on}` : "日期未指定", item.status === "candidate" ? "未进入正式简报" : `由 ${item.reviewed_by || "审核人"} 确认`].join(" · ");
    const controls = document.createElement("div"); controls.className = "watch-item-controls";
    const source = state.sources.find((row) => row.id === item.source_id && row.content_sha256 === item.source_sha256);
    const original = document.createElement("button"); original.type = "button"; original.className = "text-button"; original.textContent = source ? "查看依据 ↗" : "来源版本不可用"; original.disabled = !source;
    original.addEventListener("click", () => openSource(source, item.source_locator)); controls.append(original);
    if (item.status === "candidate" && state.capabilities.review) {
      const review = document.createElement("button"); review.type = "button"; review.className = "secondary-button"; review.textContent = "核对事项"; review.addEventListener("click", () => openWatchReview(item)); controls.append(review);
    }
    const pendingChange = pendingChanges.find((change) => change.watch_item_id === item.id);
    if (["watching", "action_required", "resolved"].includes(item.status) && !pendingChange && state.capabilities.propose) {
      const amend = document.createElement("button"); amend.type = "button"; amend.className = "secondary-button"; amend.textContent = item.status === "resolved" ? "重新打开" : "更新 / 处理"; amend.addEventListener("click", () => openWatchChange(item)); controls.append(amend);
    }
    if (state.watchChanges.some((change) => change.watch_item_id === item.id)) {
      const history = document.createElement("button"); history.type = "button"; history.className = "text-button"; history.textContent = "处理记录"; history.addEventListener("click", () => openWatchHistory(item)); controls.append(history);
    }
    row.append(heading, question, meta, controls);
    if (pendingChange) {
      const proposal = document.createElement("div"); proposal.className = "watch-change-pending";
      const target = ({ action_required: "需要推进", watching: "持续关注", resolved: "处理完成" })[pendingChange.target_status];
      proposal.append(reportNode("strong", "", `待审核变更 · ${target}`), reportNode("p", "", pendingChange.reason));
      if (state.capabilities.review) {
        const review = document.createElement("button"); review.type = "button"; review.className = "secondary-button"; review.textContent = "核对变更"; review.addEventListener("click", () => openWatchChangeReview(item, pendingChange)); proposal.append(review);
      }
      row.append(proposal);
    }
    box.append(row);
  }
}
function renderMonitoring() {
  const plansBox = $("monitoring-plans"), signalsBox = $("monitoring-signals");
  plansBox.replaceChildren(); signalsBox.replaceChildren();
  renderMonitoringDomains();
  renderCompetition();
  renderOperatingMetrics();
  renderActivityTimeline();
  renderWatchItems();
  renderLocalMaterialUpdates();
  renderMetricSeries();
  renderFinancialScreen();
  if (state.monitoringError) {
    $("public-search-starter").hidden = true;
    $("monitoring-state").textContent = "监控状态暂不可用";
    empty(plansBox, "监控状态读取失败。项目材料仍可使用，请稍后刷新状态。");
    empty(signalsBox, "公开线索暂不可用。");
    return;
  }
  const active = state.monitoringJobs.filter((job) => job.status === "queued" || job.status === "processing").length;
  const publicKinds = new Set(state.monitoringPlans.filter((plan) => { try { const url = new URL(plan.target); return (url.hostname === "api.gdeltproject.org" && url.pathname === "/api/v2/doc/doc") || (plan.kind === "brave_news" && url.hostname === "api.search.brave.com"); } catch { return false; } }).map((plan) => plan.kind));
  $("public-search-starter").hidden = !state.capabilities.manage || (publicKinds.has("rss") && publicKinds.has("brave_news"));
  $("monitoring-state").textContent = !state.monitoringPlans.length ? "尚未设置来源" : `${state.monitoringPlans.length} 个公开来源 · ${active} 个运行中 · ${state.signals.length} 条候选线索`;
  if (!state.monitoringPlans.length) empty(plansBox, "还没有订阅源。添加后可先手动检查，再决定是否开启定时检查。");
  for (const plan of state.monitoringPlans) {
    const job = state.monitoringJobs.filter((item) => item.plan_id === plan.id).at(-1);
    const row = document.createElement("article"); row.className = "monitoring-plan";
    const detail = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = targetHost(plan.target);
    const url = document.createElement("span"); url.className = "plan-url"; url.textContent = plan.target;
    const meta = document.createElement("span"); meta.className = "plan-detail";
    const counts = plan.last_run_status === "completed" && plan.last_item_count !== null
      ? ` · 抓取 ${plan.last_item_count}${plan.last_matched_count === null ? "" : ` · 名称匹配 ${plan.last_matched_count}`}` : "";
    meta.textContent = `${cadenceText(plan.cadence_minutes)} · ${plan.enabled ? "定时检查已开启" : "定时检查未开启"} · ${monitoringStatus(plan, job)}${counts}`;
    detail.append(title, url, meta);
    const actions = document.createElement("div"); actions.className = "monitoring-actions";
    const run = document.createElement("button"); run.type = "button"; run.textContent = "运行一次";
    run.disabled = job?.status === "queued" || job?.status === "processing";
    run.addEventListener("click", () => runMonitoringPlan(plan, run));
    const toggle = document.createElement("button"); toggle.type = "button";
    toggle.textContent = plan.enabled ? "关闭定时" : "开启定时";
    toggle.setAttribute("aria-pressed", String(plan.enabled));
    toggle.addEventListener("click", () => toggleMonitoringPlan(plan, toggle));
    if (state.capabilities.manage) actions.append(run, toggle);
    if (state.capabilities.manage && job?.status === "failed") {
      const retry = document.createElement("button"); retry.type = "button"; retry.className = "failed"; retry.textContent = "重试失败任务";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try { await request(`/api/projects/${state.current.id}/monitoring/jobs/${job.id}/retry`, { method: "POST" }); toast("任务已重新排队"); await refreshProject(); }
        catch (error) { toast(error.message); retry.disabled = false; }
      });
      actions.append(retry);
    }
    row.append(detail);
    if (state.capabilities.manage) row.append(actions);
    plansBox.append(row);
  }
  if (!state.signals.length) { empty(signalsBox, "暂未找到相关公开消息。可在上方查看最近检查是否成功。"); return; }
  for (const signal of state.signals) {
    const row = document.createElement("article"); row.className = "signal";
    const date = document.createElement("div"); date.className = "signal-date"; date.textContent = dateText(signal.first_seen_at);
    const content = document.createElement("div"); content.className = "signal-content";
    const link = document.createElement("a"); link.href = signal.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = signal.title;
    const summary = document.createElement("p"); summary.textContent = signal.summary || "来源未提供摘要。";
    const meta = document.createElement("div"); meta.className = "signal-meta";
    meta.textContent = `原文：${targetHost(signal.url)} · 发布于 ${signal.published_at ? dateText(signal.published_at) : "日期不明"}`;
    const flag = document.createElement("span"); flag.className = "signal-flag";
    flag.textContent = signal.freshness === "historical" ? "历史内容" : signal.freshness === "unknown" ? "日期不明" : "近期线索";
    meta.append(flag); content.append(link, summary, meta); row.append(date, content); signalsBox.append(row);
  }
}
async function runMonitoringPlan(plan, button) {
  button.disabled = true;
  const projectId = state.current.id;
  const key = state.runKeys.get(plan.id) || crypto.randomUUID();
  state.runKeys.set(plan.id, key);
  try {
    await request(`/api/projects/${projectId}/monitoring/plans/${plan.id}/runs`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: key }),
    });
    state.runKeys.delete(plan.id); toast("已加入后台检查队列"); await refreshProject();
  } catch (error) { toast(error.message); button.disabled = false; }
}
async function toggleMonitoringPlan(plan, button) {
  button.disabled = true;
  try {
    await request(`/api/projects/${state.current.id}/monitoring/plans/${plan.id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !plan.enabled }),
    });
    await refreshProject(); toast(plan.enabled ? "已关闭定时检查" : "已开启定时检查");
  } catch (error) { toast(error.message); button.disabled = false; }
}
function renderMaterials() {
  const recent = $("recent-materials"), full = $("material-list");
  recent.replaceChildren(); full.replaceChildren();
  const sources=materialSelection(state.sources,state.current?.id),search=$("material-search"),filter=$("material-topic");
  if(full.dataset.projectId!==state.current?.id){search.value="";filter.value="";full.dataset.projectId=state.current?.id||"";}
  const chosen=filter.value;filter.replaceChildren(new Option("所有分类", ""),...[...new Set(sources.map(s=>s.topic).filter(Boolean))].sort().map(topic=>new Option(topic,topic)));
  filter.value=[...filter.options].some(option=>option.value===chosen)?chosen:"";
  if (!sources.length) { empty(recent, "暂无材料"); empty(full, "还没有项目资料。拖入文件，或点击“添加资料”。"); applyMaterialFilter(); return; }
  for (const source of sources) {
    const job = jobFor(source);
    const basis = source.topic_basis === "filename" ? "按文件名" : source.topic_basis === "content" ? "按内容" : source.topic_basis === "filename_and_content" ? "按文件名与内容" : "";
    const subtitle = `${statusText(job)} · ${dateText(source.created_at)}${basis ? ` · ${basis}归类` : ""}`;
    const open = () => openSource(source);
    const wrapper = document.createElement("div");wrapper.className="material-record";wrapper.dataset.sourceId=source.id;wrapper.dataset.topic=source.topic||"";
    const row=makeRow("material-row", source.reference, `${source.topic||"待整理"} · ${subtitle}`, "阅读正文 ↗", open);
    const format=document.createElement("span");format.className="file-format";format.textContent=source.reference.split('.').at(-1).toUpperCase().slice(0,5);format.setAttribute("aria-hidden","true");row.prepend(format);wrapper.append(row);
    const earlier = sources.filter((item) => item.id!==source.id && item.created_at<source.created_at &&
      item.kind === "document" && item.reference === source.reference && jobFor(item)?.status === "completed"
    ).sort((a,b)=>a.created_at.localeCompare(b.created_at)).at(-1);
    if (source.kind === "document" && job?.status === "completed" && earlier) {
      const compare = document.createElement("button"); compare.type = "button"; compare.className = "compare-link";
      compare.textContent = "与同名旧材料比较 ↗";
      compare.addEventListener("click", () => openComparison(earlier, source));
      wrapper.append(compare);
    }
    if (state.capabilities.propose && job?.status === "failed") {
      const retry = document.createElement("button"); retry.type = "button"; retry.className = "retry";
      retry.textContent = `重试解析 · ${job.error_code || "错误"}`;
      retry.addEventListener("click", async () => { try { await request(`/api/projects/${state.current.id}/intake/${job.id}/retry`, { method: "POST" }); toast("已重新排队"); refreshProject(); } catch (error) { toast(error.message); } });
      wrapper.append(retry);
    }
    full.append(wrapper);
  }
  applyMaterialFilter();
  for (const source of sources.slice(0,4)) {
    const job = jobFor(source);
    recent.append(makeRow("material-row", source.reference,
      `${statusText(job)} · ${dateText(source.created_at)}`, "阅读 ↗", () => openSource(source)));
  }
}
function renderObservations(candidates, confirmed, rejected) {
  const pendingBox = $("candidate-list"), confirmedBox = $("confirmed-list"), rejectedBox = $("rejected-list");
  const impactBox = $("impact-list"), reviewedBox = $("reviewed-impact-list");
  pendingBox.replaceChildren(); confirmedBox.replaceChildren(); rejectedBox.replaceChildren(); impactBox.replaceChildren(); reviewedBox.replaceChildren();
  const candidateVersions = new Map(candidates.map((item) => [item.id, item.version]));
  const candidateIds = new Set(candidateVersions.keys());
  for (const id of state.selectedCandidates) if (!candidateIds.has(id)) state.selectedCandidates.delete(id);
  for (const [id, draft] of state.batchDecisions) if (candidateVersions.get(id) !== draft.version) state.batchDecisions.delete(id);
  updateBatchReviewToolbar(candidates.length);
  const pendingImpacts = state.impacts.filter((item) => item.status === "pending");
  const reviewedImpacts = state.impacts.filter((item) => item.status === "reviewed");
  if (!pendingImpacts.length) empty(impactBox, "暂无需要回看的旧判断。未分类或跨主题材料仍需人工浏览。");
  if (!reviewedImpacts.length) empty(reviewedBox, "尚无复核记录。");
  for (const impact of pendingImpacts) {
    const observation = confirmed.find((item) => item.id === impact.observation_id);
    const source = state.sources.find((item) => item.id === impact.trigger_source_id);
    impactBox.append(makeRow("observation-row", observation ? observationTitle(observation) : "旧判断待查",
      `${impact.topic} · 新材料：${source?.reference || "来源待查"}`, state.capabilities.review ? "回看 →" : "阅读新材料 ↗",
      () => state.capabilities.review ? openImpactReview(impact) : openSource(source)));
  }
  for (const impact of reviewedImpacts) {
    const observation = confirmed.find((item) => item.id === impact.observation_id);
    reviewedBox.append(makeRow("observation-row", observation ? observationTitle(observation) : "旧判断待查",
      `${impact.resolution === "unchanged" ? "保留原判断" : "继续查证"} · ${impact.note || ""} · ${dateText(impact.reviewed_at)}`,
      "查看来源 ↗", () => openSource(state.sources.find((item) => item.id === impact.trigger_source_id))));
  }
  if (!candidates.length) empty(pendingBox, "目前没有待核对的候选。后台只提取短摘录，不会自动确认事实。");
  if (!confirmed.length) empty(confirmedBox, "尚无经过人工确认的事实。");
  if (!rejected.length) empty(rejectedBox, "尚无驳回记录。");
  const lowInfo = document.createElement("details"); lowInfo.className = "low-information-candidates";
  const lowInfoCount = candidates.filter((item) => item.quality_hint).length;
  lowInfo.append(reportNode("summary", "", `低信息摘录 · ${lowInfoCount} 条（保留原记录）`));
  lowInfo.append(reportNode("p", "muted", "表头、目录或通用表述。这里只调整展示，不删除、不驳回；展开后仍可核对。"));
  for (const observation of candidates) {
    const destination = observation.quality_hint ? lowInfo : pendingBox;
    const source = state.sources.find((item) => item.id === observation.source_id);
    const row = makeRow("observation-row", observationTitle(observation),
      `${observation.quality_hint ? observation.quality_hint + " · " : ""}${observation.subject} · ${source?.reference || "来源待查"} · ${observation.source_locator}`, state.capabilities.review ? "核对 →" : "阅读 ↗", () => state.capabilities.review ? openReview(observation) : openSource(source));
    if (!state.capabilities.review) { destination.append(row); continue; }
    const wrapper = document.createElement("div"); wrapper.className = "candidate-select-row";
    const label = document.createElement("label"); label.className = "candidate-select";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = state.selectedCandidates.has(observation.id);
    checkbox.setAttribute("aria-label", `选择候选：${observation.statement}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && state.selectedCandidates.size >= 20) { checkbox.checked = false; toast("每批最多核对 20 条"); return; }
      if (checkbox.checked) state.selectedCandidates.add(observation.id);
      else { state.selectedCandidates.delete(observation.id); state.batchDecisions.delete(observation.id); }
      updateBatchReviewToolbar(candidates.length);
    });
    label.append(checkbox); wrapper.append(label, row); destination.append(wrapper);
  }
  if (lowInfoCount) pendingBox.append(lowInfo);
  for (const observation of confirmed) {
    const source = state.sources.find((item) => item.id === observation.source_id);
    confirmedBox.append(makeRow("observation-row", observationTitle(observation),
      `${observation.subject} · ${source?.reference || "来源待查"} · ${observation.source_locator}`, "已确认", () => openSource(source)));
  }
  for (const observation of rejected) {
    const source = state.sources.find((item) => item.id === observation.source_id);
    rejectedBox.append(makeRow("observation-row", observationTitle(observation),
      `${observation.subject} · ${source?.reference || "来源待查"} · ${observation.source_locator}`, "查看原件 ↗", () => openSource(source)));
  }
}
function applyMaterialFilter() {
  const full=$("material-list"),selection=materialSelection(state.sources,state.current?.id,{query:$("material-search").value,topic:$("material-topic").value});
  const ids=new Set(selection.map(source=>source.id));
  for(const record of full.querySelectorAll('.material-record'))record.hidden=!ids.has(record.dataset.sourceId);
  $("material-filter-count").textContent=`${selection.length} / ${materialSelection(state.sources,state.current?.id).length} 份 · 最近收到在前`;
  $("material-no-results").hidden=selection.length>0||!state.sources.some(source=>source.project_id===state.current?.id);
}
function updateBatchReviewToolbar(candidateCount) {
  $("batch-review-toolbar").hidden = !state.capabilities.review || !candidateCount;
  $("batch-review-count").textContent = `已选 ${state.selectedCandidates.size} / 20`;
  $("batch-review-open").disabled = !state.selectedCandidates.size;
}
function observationTitle(observation) {
  const basis = { actual: "实际", forecast: "预测", budget: "预算", unknown: "未判定" }[observation.basis] || "未判定";
  return observation.number === null ? observation.statement : `${observation.statement} · ${observation.number} ${observation.unit || ""} · ${observation.period || "期间待查"} · ${basis}`;
}
function syncCapitalMetricHint() {
  const capital = $("metric-subject").value === "资本与持仓";
  $("capital-metric-hint").hidden = !capital;
  $("metric-scope").placeholder = capital ? "本机构 / 项目公司" : "例如：合并报表";
}
function openMetricDraft(source, fragment, suggestion = null) {
  state.metricDraft = { projectId: state.current.id, sourceId: source.id, sourceSha256: source.content_sha256, locator: fragment.locator };
  $("metric-form").reset();
  if (suggestion) {
    for (const [field, value] of Object.entries({ name: suggestion.metric, number: suggestion.number, unit: suggestion.unit, period: suggestion.period, scope: suggestion.scope, basis: suggestion.basis })) {
      $( `metric-${field}` ).value = value || "";
    }
  }
  syncCapitalMetricHint();
  $("metric-proof").textContent = `${source.reference} · ${fragment.locator} · SHA-256 ${source.content_sha256.slice(0, 12)}…\n${fragment.text.slice(0, 240)}`;
  if (suggestion) $("metric-proof").textContent += `\n数值位置：${suggestion.source_cell} · 列名：${suggestion.column_header || "待核对"}\n表头依据：${suggestion.context_locators.join("；")}`;
  $("metric-dialog").showModal(); $("metric-name").focus();
}
function openHoldingDraft(source, fragment) {
  state.holdingDraft = { projectId: state.current.id, sourceId: source.id, sourceSha256: source.content_sha256, locator: fragment.locator };
  $("holding-form").reset();
  $("holding-proof").textContent = `${source.reference} · ${fragment.locator} · SHA-256 ${source.content_sha256.slice(0, 12)}…\n${fragment.text.slice(0, 320)}`;
  $("holding-dialog").showModal(); $("holding-fund").focus();
}
function openHoldingReview(item) {
  state.holdingReview = item;
  const source = state.sources.find((row) => row.id === item.source_id);
  $("holding-review-summary").textContent = `${item.fund_name} · ${item.as_of} · 成本 ${item.invested_cost_wan ?? "未登记"} 万元 · 持股 ${item.ownership_pct ?? "未登记"}% · 价值 ${item.current_value_wan ?? "未登记"} 万元`;
  $("holding-review-source").textContent = `${source?.reference || "来源不可用"} · ${item.source_locator}`;
  $("holding-preview-reviewer").hidden = state.teamMode;
  $("holding-reviewer").required = !state.teamMode;
  $("holding-review-form").reset();
  $("holding-review-dialog").showModal();
}
async function openSource(source, focusLocator = null) {
  if (!source) return;
  const projectId = state.current.id;
  if ($("reader").hidden || !$("reader").contains(document.activeElement)) {
    readerReturn = { projectId, view: state.view, restore: captureReturnFocus($("project-screen")) };
  }
  state.selectedSource = source.id;
  $("reader").hidden = false; $("reader-title").textContent = source.reference;
  $("reader-label").textContent="提取正文 · 非原版排版";$("reader-source-info").open=false;
  $("reader-meta").textContent = `来源版本 ${source.id.slice(0, 8)} · ${dateText(source.created_at)} · SHA-256 ${source.content_sha256.slice(0, 12)}…`;
  const body = $("reader-content"); empty(body, "正在读取提取文本…");
  try {
    const isSpreadsheet = source.kind === "document" && /\.xlsx?$/i.test(source.reference) && jobFor(source)?.status === "completed";
    const [fragments, mapping, operatingMapping] = await Promise.all([
      request(`/api/projects/${projectId}/sources/${source.id}/fragments`),
      isSpreadsheet ? request(`/api/projects/${projectId}/sources/${source.id}/financial-mapping`).catch(() => null) : Promise.resolve(null),
      isSpreadsheet ? request(`/api/projects/${projectId}/sources/${source.id}/operating-mapping`).catch(() => null) : Promise.resolve(null),
    ]);
    if (state.current?.id !== projectId || state.selectedSource !== source.id) return;
    body.replaceChildren();
    if (!fragments.length) { empty(body, `${statusText(jobFor(source))}。当前没有可阅读的提取文本。`); return; }
    if (jobFor(source)?.status === "needs_ocr") {
      const warning = document.createElement("p"); warning.className = "preview-warning";
      warning.textContent = `已提取 ${fragments.length} 个片段，仍有页面未识别完整。以下不是完整原件；不要把缺失内容当作企业未披露。`;
      body.append(warning);
    }
    if (isSpreadsheet) renderFinancialMappingSummary(body, mapping, source, fragments);
    if (isSpreadsheet) renderOperatingMappingSummary(body, operatingMapping);
    let focused = null;
    for (const fragment of fragments) {
      const section = document.createElement("section"); section.className = "fragment";
      section.dataset.locator = fragment.locator;
      const locator = document.createElement("div"); locator.className = "fragment-locator"; locator.textContent = fragment.locator;
      const text = document.createElement("p"); text.textContent = fragment.text;
      section.append(locator, text);
      const actions=document.createElement("details");actions.className="fragment-actions";const summary=document.createElement("summary");summary.textContent="更多操作";actions.append(summary);
      if (!state.teamMode && state.capabilities.propose && source.kind === "document" && fragment.text.trim()) {
        const ai = document.createElement("button"); ai.type = "button"; ai.className = "fragment-metric"; ai.textContent = "AI 提取";
        ai.addEventListener("click", () => openAiExtraction(source, fragment)); actions.append(ai);
      }
      if (state.capabilities.propose && source.kind === "document" && jobFor(source)?.status === "completed" && /\d/.test(fragment.text)) {
        const add = document.createElement("button"); add.type = "button"; add.className = "fragment-metric"; add.textContent = "记录指标 +";
        add.addEventListener("click", () => openMetricDraft(source, fragment)); actions.append(add);
        const position = document.createElement("button"); position.type = "button"; position.className = "fragment-metric"; position.textContent = "登记持仓 +";
        position.addEventListener("click", () => openHoldingDraft(source, fragment)); actions.append(position);
      }
      if(actions.childElementCount>1)section.append(actions);
      if (focusLocator === fragment.locator) { section.classList.add("fragment-focused"); focused = section; }
      body.append(section);
    }
    if (focused) requestAnimationFrame(() => focused.scrollIntoView({ block: "center", behavior: "smooth" }));
  } catch (error) { empty(body, error.message); }
}
function renderFinancialMappingSummary(body, mapping, source, fragments) {
  const panel = document.createElement("section"); panel.className = "financial-mapping-summary";
  const title = document.createElement("div"); title.className = "financial-mapping-heading";
  const label = document.createElement("strong"); label.textContent = "财务字段识别";
  const stateLabel = document.createElement("span");
  stateLabel.textContent = mapping ? `${mapping.review_drafts?.length || 0} 项可核对 · ${mapping.candidate_count} 项满足自动候选条件` : "识别状态暂不可用";
  title.append(label, stateLabel); panel.append(title);
  const note = document.createElement("p");
  note.textContent = !mapping ? "原表仍可阅读和手动记录。" : mapping.truncated_count ? "自动建议有数量上限，未列入的项目请从原表核对并手动记录。" : mapping.candidate_count ? "自动识别只形成候选，复核状态见待核对与已确认清单。" : "未自动认定数值；可以阅读原表后手动记录。";
  panel.append(note);
  renderFinancialReviewDrafts(panel, note, mapping, source, fragments);
  if (mapping?.issues.length) {
    const reasons = { scope_or_unit_missing: "未明确合并口径", unit_missing: "缺少单位", currency_unsupported: "非人民币口径，待人工核对", period_missing: "缺少明确期间", basis_ambiguous: "实际与预测口径冲突", trial_balance_not_closing: "科目余额表仅识别明确的期末余额" };
    const list = document.createElement("div"); list.className = "financial-mapping-issues";
    for (const issue of mapping.issues.slice(0, 4)) {
      const link = document.createElement("button"); link.type = "button";
      link.textContent = `${reasons[issue.reason] || "需核对"} · ${issue.source_locator} ↗`;
      link.addEventListener("click", () => {
        const target = [...body.querySelectorAll(".fragment")].find((fragment) => fragment.dataset.locator === issue.source_locator);
        target?.scrollIntoView({ block: "center", behavior: "smooth" });
      });
      list.append(link);
    }
    if (mapping.issues.length > 4) {
      const more = document.createElement("small"); more.textContent = `另有 ${mapping.issues.length - 4} 处，最多展示前 40 处。`;
      list.append(more);
    }
    panel.append(list);
  }
  body.append(panel);
}
function renderFinancialReviewDrafts(panel, note, mapping, source, fragments) {
  if (mapping?.review_drafts?.length) {
    note.textContent = "以下为原表数值草稿，尚未保存为事实。核对单位、期间和范围后可提出候选；缺少口径不会自动当作合并实际数。";
    const wrap = document.createElement("div"); wrap.className = "financial-draft-table";
    const table = document.createElement("table"); table.setAttribute("aria-label", "待核对财务字段");
    const head = document.createElement("thead"); const titles = document.createElement("tr");
    for (const label of ["科目 / 定位", "数值", "期间 / 列名", "待补充", ""]) titles.append(reportNode("th", "", label));
    head.append(titles); table.append(head);
    const rows = document.createElement("tbody");
    const names = { unit: "单位", period: "期间", scope: "范围", basis: "实际/预测" };
    for (const draft of mapping.review_drafts) {
      const fragment = fragments.find((item) => item.locator === draft.source_locator);
      if (!fragment) continue;
      const row = document.createElement("tr");
      const label = document.createElement("td"); label.append(reportNode("strong", "", draft.metric), reportNode("small", "", `${draft.source_locator} · ${draft.source_cell}`));
      const value = reportNode("td", "", `${draft.number} ${draft.unit || "单位待核"}`);
      const period = document.createElement("td"); period.append(reportNode("span", "", draft.period || "待核对"), reportNode("small", "", draft.column_header || "列名未识别"));
      const missing = reportNode("td", "", draft.missing_fields.map((key) => names[key] || key).join("、") || "完整，仍需核对");
      const action = document.createElement("td");
      if (state.capabilities.propose) {
        const button = reportNode("button", "fragment-metric", "核对并补充"); button.type = "button";
        button.setAttribute("aria-label", `核对 ${draft.metric} ${draft.source_cell}`);
        button.addEventListener("click", () => openMetricDraft(source, fragment, draft)); action.append(button);
      }
      row.append(label, value, period, missing, action); rows.append(row);
    }
    table.append(rows); wrap.append(table); panel.append(wrap);
    if (mapping.review_truncated_count) panel.append(reportNode("p", "muted", `另有 ${mapping.review_truncated_count} 项未展开，请继续核对原表。`));
  }
}
function renderOperatingMappingSummary(body, mapping) {
  if (!mapping || (!mapping.candidate_count && !mapping.issues.length)) return;
  const panel = document.createElement("section"); panel.className = "financial-mapping-summary";
  const title = document.createElement("div"); title.className = "financial-mapping-heading";
  const label = document.createElement("strong"); label.textContent = "经营指标识别";
  const stateLabel = document.createElement("span");
  stateLabel.textContent = `识别到 ${mapping.candidate_count} 项 · ${mapping.issues.length} 处待核对${mapping.truncated_count ? ` · ${mapping.truncated_count} 项未列入建议` : ""}`;
  title.append(label, stateLabel); panel.append(title);
  const note = document.createElement("p");
  note.textContent = "仅提取表内明确标注的产品、指标、单位和期间；数值仍需人工复核。";
  panel.append(note);
  if (mapping.issues.length) {
    const reasons = { scope_missing: "缺产品或产线", unit_missing: "单位缺失或不支持", period_missing: "期间不明", basis_missing: "实际/预测口径不明", basis_ambiguous: "实际与预测口径冲突", output_not_actual: "实际产量不可用预测数", invalid_utilization: "利用率超出有效范围", invalid_quantity: "数量无效", percent_scale_ambiguous: "百分比显示尺度不明" };
    const list = document.createElement("div"); list.className = "financial-mapping-issues";
    for (const issue of mapping.issues.slice(0, 4)) {
      const link = document.createElement("button"); link.type = "button";
      link.textContent = `${reasons[issue.reason] || "需核对"} · ${issue.source_locator} ↗`;
      link.addEventListener("click", () => {
        const target = [...body.querySelectorAll(".fragment")].find((fragment) => fragment.dataset.locator === issue.source_locator);
        target?.scrollIntoView({ block: "center", behavior: "smooth" });
      });
      list.append(link);
    }
    if (mapping.issues.length > 4) {
      const more = document.createElement("small"); more.textContent = `另有 ${mapping.issues.length - 4} 处，最多展示前 40 处。`;
      list.append(more);
    }
    panel.append(list);
  }
  body.append(panel);
}
async function openComparison(before, after) {
  const selection = `${before.id}:${after.id}`;
  state.selectedSource = selection;
  $("reader").hidden = false;
  $("reader-label").textContent="提取正文对照";$("reader-source-info").open=false;
  $("reader-title").textContent = `版本变化 · ${after.reference}`;
  $("reader-meta").textContent = `同名建议配对 · 旧版 ${before.content_sha256.slice(0, 8)} / 新版 ${after.content_sha256.slice(0, 8)}`;
  const body = $("reader-content"); empty(body, "正在比较提取文本…");
  try {
    const path = `/api/projects/${state.current.id}/sources/${after.id}/compare?before_source_id=${encodeURIComponent(before.id)}`;
    const result = await request(path);
    if (state.selectedSource !== selection) return;
    body.replaceChildren();
    const note = document.createElement("p"); note.className = "compare-note";
    note.textContent = "按原件定位比较提取文本。同名不等于已核验版本关系；这里的差异不是已确认事实。";
    const summary = document.createElement("div"); summary.className = "compare-summary";
    summary.textContent = `${result.counts.changed} 处改动 · ${result.counts.added} 处新增 · ${result.counts.removed} 处移除`;
    body.append(note, summary);
    if (!result.changes.length) {
      const noChange = document.createElement("p"); noChange.className = "empty";
      noChange.textContent = result.truncated ? "已比较范围内没有文本差异，仍有未覆盖位置。" : "两份材料的提取文本没有差异。";
      body.append(noChange);
    }
    for (const change of result.changes) {
      const section = document.createElement("section"); section.className = "compare-change";
      const heading = document.createElement("h3");
      heading.textContent = `${change.locator} · ${{ added: "新增", changed: "改动", removed: "移除" }[change.kind]}`;
      section.append(heading);
      for (const [label, value] of [["旧版", change.before], ["新版", change.after]]) {
        if (value === null) continue;
        const column = document.createElement("div"); column.className = "compare-excerpt";
        const title = document.createElement("span"); title.textContent = label;
        const text = document.createElement("p"); text.textContent = value;
        column.append(title, text); section.append(column);
      }
      body.append(section);
    }
    if (result.truncated) {
      const warning = document.createElement("p"); warning.className = "compare-note";
      warning.textContent = `仅展示部分差异：已比较 ${result.compared_locations}/${result.total_locations} 个定位，最多显示 100 处。请阅读原件核对。`;
      body.append(warning);
    }
    if (result.excerpt_truncated) {
      const warning = document.createElement("p"); warning.className = "compare-note";
      warning.textContent = "每处只显示前 500 字；需要完整上下文时请打开原件阅读。";
      body.append(warning);
    }
  } catch (error) { empty(body, `比较暂不可用：${error.message}`); }
}
function openReview(observation) {
  state.review = observation;
  const source = state.sources.find((item) => item.id === observation.source_id);
  $("review-statement").textContent = observation.statement;
  $("review-metric").hidden = observation.number === null;
  if (observation.number !== null) $("review-metric").textContent = `${observation.number} ${observation.unit || ""} · ${observation.period || "期间待查"} · ${{ actual: "实际", forecast: "预测", budget: "预算", unknown: "未判定" }[observation.basis] || "未判定"}${observation.scope ? ` · ${observation.scope}` : ""}`;
  $("review-evidence").textContent = `来源：${source?.reference || "未知"} · 定位：${observation.source_locator} · 当前版本：${observation.version}`;
  const projectId = state.current.id;
  $("review-original").textContent = "正在读取对应原文…";
  if (source) request(`/api/projects/${projectId}/sources/${source.id}/fragments`).then(fragments => {
    if (state.current?.id !== projectId || state.review?.id !== observation.id) return;
    const fragment = fragments.find(item => item.locator === observation.source_locator);
    $("review-original").textContent = fragment?.text || "未找到对应片段。请打开来源，核对完整上下文。";
  }).catch(() => { if (state.current?.id === projectId && state.review?.id === observation.id) $("review-original").textContent = "原文读取失败，请打开来源重试。"; });
  else $("review-original").textContent = "来源尚不可用，请先检查材料。";
  $("review-dialog").showModal();
  if (state.teamMode) $("review-form").querySelector("[type=submit]").focus(); else $("reviewer-input").focus();
}
function openBatchReview() {
  if (!state.current || !state.capabilities.review) return;
  const selected = state.observations.filter((item) => item.status === "candidate" && state.selectedCandidates.has(item.id));
  if (!selected.length) { toast("先选择待核对摘录"); return; }
  if (selected.length > 20) { toast("每批最多核对 20 条"); return; }
  const list = $("batch-review-list"); list.replaceChildren();
  $("batch-review-form").dataset.projectId = state.current.id;
  selected.forEach((observation, index) => {
    const source = state.sources.find((item) => item.id === observation.source_id);
    const item = document.createElement("section"); item.className = "batch-review-item";
    item.dataset.observationId = observation.id;
    item.dataset.version = String(observation.version);
    const number = document.createElement("span"); number.className = "batch-review-number"; number.textContent = String(index + 1).padStart(2, "0");
    const content = document.createElement("div"); content.className = "batch-review-content";
    const title = document.createElement("strong"); title.textContent = observationTitle(observation);
    const context = document.createElement("p"); context.textContent = `${observation.subject} · ${source?.reference || "来源待查"} · ${observation.source_locator} · 版本 ${observation.version}`;
    const sourceButton = document.createElement("button"); sourceButton.type = "button"; sourceButton.className = "text-button"; sourceButton.textContent = "阅读来源定位 ↗";
    sourceButton.addEventListener("click", () => {
      $("batch-review-dialog").close(); openSource(source, observation.source_locator);
    });
    const label = document.createElement("label"); label.textContent = `第 ${index + 1} 条处理结果`;
    const select = document.createElement("select"); select.required = true; select.setAttribute("aria-label", label.textContent);
    for (const [value, text] of [["", "请选择"], ["confirm", "确认候选"], ["reject", "驳回候选"]]) {
      const option = document.createElement("option"); option.value = value; option.textContent = text; select.append(option);
    }
    const draft = state.batchDecisions.get(observation.id);
    if (draft?.version === observation.version) select.value = draft.action;
    select.addEventListener("change", () => {
      if (select.value) state.batchDecisions.set(observation.id, { version: observation.version, action: select.value });
      else state.batchDecisions.delete(observation.id);
    });
    label.append(select); content.append(title, context, sourceButton, label); item.append(number, content); list.append(item);
  });
  $("batch-review-dialog").showModal();
  list.querySelector("select")?.focus();
}
function openImpactReview(impact) {
  state.impactReview = impact;
  const observation = state.observations.find((item) => item.id === impact.observation_id);
  const source = state.sources.find((item) => item.id === impact.trigger_source_id);
  $("impact-statement").textContent = observation?.statement || "旧判断待查";
  $("impact-evidence").textContent = `新材料：${source?.reference || "来源待查"} · 主题：${impact.topic}。这是回看提示，不表示旧判断已被推翻。`;
  $("impact-dialog").showModal();
  $("impact-resolution").focus();
}
function openWatchCreate() {
  if (!state.current || !state.capabilities.propose) return;
  const observations = state.observations.filter((row) => row.status === "confirmed" && row.source_locator && state.sources.some((source) => source.id === row.source_id));
  if (!observations.length) { toast("先核对一条有原件定位的观察，再建立关注事项。"); return; }
  const select = $("watch-observation"); select.replaceChildren();
  for (const observation of observations) {
    const option = document.createElement("option"); option.value = observation.id;
    option.textContent = `${observation.subject} · ${observation.statement.slice(0, 72)}`;
    select.append(option);
  }
  $("watch-create-dialog").showModal(); select.focus();
}
function openWatchReview(item) {
  if (!state.capabilities.review || item.status !== "candidate") return;
  state.watchReview = item;
  $("watch-review-title").textContent = item.title;
  $("watch-review-question").textContent = item.question;
  $("watch-review-evidence").textContent = `${item.source_reference} · ${item.source_locator} · SHA ${item.source_sha256.slice(0, 12)} · 观察版本 ${item.observation_version}`;
  $("watch-review-action").value = "watch";
  $("watch-review-dialog").showModal(); $("watch-review-action").focus();
}
function openWatchChange(item) {
  if (!state.current || !state.capabilities.propose || ["candidate", "rejected"].includes(item.status)) return;
  if (state.watchChanges.some((change) => change.watch_item_id === item.id && change.status === "candidate")) { toast("此事项已有待审核变更。"); return; }
  state.watchChange = item;
  $("watch-change-form").reset();
  $("watch-change-title").textContent = item.status === "resolved" ? `重新打开 · ${item.title}` : `更新 · ${item.title}`;
  $("watch-change-target").querySelector('[value="resolved"]').disabled = item.status === "resolved";
  $("watch-change-target").value = item.status === "resolved" ? "watching" : item.status;
  $("watch-change-question").value = item.question;
  $("watch-change-owner").value = item.owner || "";
  $("watch-change-due").value = item.due_on || "";
  const evidence = $("watch-change-evidence"); evidence.replaceChildren();
  const none = document.createElement("option"); none.value = ""; none.textContent = "沿用当前依据"; evidence.append(none);
  for (const observation of state.observations.filter((row) => row.status === "confirmed" && row.source_locator && row.id !== item.current_observation_id)) {
    const option = document.createElement("option"); option.value = observation.id;
    option.textContent = `${observation.subject} · ${observation.statement.slice(0, 65)}`; evidence.append(option);
  }
  $("watch-change-dialog").showModal(); $("watch-change-target").focus();
}
function openWatchChangeReview(item, change) {
  if (!state.capabilities.review || change.status !== "candidate") return;
  state.watchChangeReview = { item, change };
  $("watch-change-review-form").reset();
  $("watch-change-review-title").textContent = item.title;
  const target = ({ action_required: "需要推进", watching: "持续关注", resolved: "本轮处理完成" })[change.target_status];
  $("watch-change-review-summary").textContent = `${target} · ${change.question} · ${change.owner ? `负责人 ${change.owner}` : "负责人未指定"} · ${change.due_on || "日期未指定"}`;
  const observation = state.observations.find((row) => row.id === (change.evidence_observation_id || item.current_observation_id));
  const source = state.sources.find((row) => row.id === observation?.source_id);
  $("watch-change-review-evidence").textContent = `变更原因：${change.reason}\n依据：${source?.reference || "来源待查"} · ${observation?.source_locator || "定位待查"}`;
  $("watch-change-review-source").disabled = !source;
  $("watch-change-review-dialog").showModal(); $("watch-change-review-action").focus();
}
function openWatchHistory(item) {
  $("watch-history-title").textContent = `处理记录 · ${item.title}`;
  const list = $("watch-history-list"); list.replaceChildren();
  const initial = document.createElement("article"); initial.className = "watch-history-entry";
  initial.append(reportNode("strong", "", "初始事项"), reportNode("p", "", item.initial_question), reportNode("small", "", [item.initial_owner ? `负责人 ${item.initial_owner}` : "负责人未指定", item.initial_due_on || "日期未指定"].join(" · ")));
  list.append(initial);
  const changes = state.watchChanges.filter((change) => change.watch_item_id === item.id).sort((a, b) => a.created_at.localeCompare(b.created_at));
  for (const change of changes) {
    const entry = document.createElement("article"); entry.className = "watch-history-entry";
    const target = ({ action_required: "需要推进", watching: "持续关注", resolved: "本轮处理完成" })[change.target_status];
    const status = ({ candidate: "待审核", accepted: "已接受", rejected: "已驳回" })[change.status];
    entry.append(reportNode("strong", "", `${target} · ${status}`), reportNode("p", "", change.question), reportNode("p", "", `原因：${change.reason}`));
    entry.append(reportNode("small", "", [change.owner ? `负责人 ${change.owner}` : "负责人未指定", change.due_on || "日期未指定", `提出于 ${dateText(change.created_at)}`, change.reviewed_by ? `审核人 ${change.reviewed_by}` : ""].filter(Boolean).join(" · ")));
    if (change.review_note) entry.append(reportNode("p", "", `审核说明：${change.review_note}`));
    list.append(entry);
  }
  $("watch-history-dialog").showModal();
}
function schedulePoll() {
  clearTimeout(state.timer);
  if (state.screen !== "project") return;
  const intakeActive = state.jobs.some((job) => job.status === "queued" || job.status === "processing");
  const monitorActive = state.monitoringJobs.some((job) => job.status === "queued" || job.status === "processing");
  if (intakeActive || monitorActive) state.timer = setTimeout(refreshProject, 1500);
  else if (state.reading?.background?.poll || state.externalAnalysis?.processing?.poll || (["overview", "changes", "external"].includes(state.view) && state.monitoringPlans.some((plan) => plan.enabled))) state.timer = setTimeout(refreshProject, 10000);
}
async function uploadFiles(files) {
  if (state.screen !== "project") { toast("请先进入要接收材料的项目"); return; }
  if (!state.current) { toast("请先建立项目"); return; }
  if (!state.capabilities.propose) { toast("当前账号不能添加材料"); return; }
  if (!files.length) return;
  if (uploadActive) { toast("上一批材料仍在上传，请稍后再添加"); return; }
  const projectId = state.current.id;
  const projectName = state.current.name;
  const panel = $("upload-status");
  const close = $("upload-status-close");
  const failures = $("upload-failures");
  uploadActive = true;
  panel.hidden = false;
  close.hidden = true;
  failures.hidden = true;
  failures.open = false;
  $("upload-failures-list").replaceChildren();
  const renderProgress = (progress, complete = false) => {
    if (state.teamMode && !state.teamSession) { panel.hidden = true; return; }
    panel.hidden = false;
    $("upload-status-title").textContent = complete
      ? `${progress.accepted} 份材料已加入 ${projectName}`
      : `正在加入 ${projectName} 的材料`;
    $("upload-status-detail").textContent = complete
      ? progress.stopped
        ? `中断于 ${progress.attempted}/${progress.total}；${progress.notSent} 份尚未发送。已加入的材料仍在后台解析。`
        : `${progress.attempted}/${progress.total} 已处理；解析在后台继续。`
      : `${progress.attempted}/${progress.total} 已处理 · 请保持页面打开`;
    $("upload-progress").max = Math.max(progress.total, 1);
    $("upload-progress").value = progress.attempted;
  };
  try {
    const result = await uploadBatch(files, async (file) => {
      const form = new FormData();
      form.append("file", file);
      await request(`/api/projects/${projectId}/files`, { method: "POST", body: form });
    }, renderProgress);
    renderProgress(result, true);
    close.hidden = false;
    if (result.failures.length) {
      failures.hidden = false;
      $("upload-failures-title").textContent = `${result.failures.length} 份未加入 · 查看原因`;
      const rows = result.failures.map(({ name, reason }) => {
        const row = document.createElement("li");
        row.textContent = `${name}：${reason.slice(0, 180)}`;
        return row;
      });
      $("upload-failures-list").replaceChildren(...rows);
    }
    if (state.current?.id === projectId && (!state.teamMode || state.teamSession)) {
      try { await refreshProject(); } catch (error) { toast(error.message); }
    }
  } finally {
    uploadActive = false;
  }
}

$("upload-status-close").addEventListener("click", () => { if (!uploadActive) $("upload-status").hidden = true; });

for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => { client.navigate(state.current.id, button.dataset.view); });
$("report-refresh").addEventListener("click", loadReport);
$("report-print").addEventListener("click", () => window.print());
$("local-material-all").addEventListener("click", () => { state.view = "materials"; renderView(); schedulePoll(); });
$("activity-review").addEventListener("click", () => { state.view = "review"; renderView(); schedulePoll(); });
$("project-more").addEventListener("click", loadMoreProjects);
$("project-members").addEventListener("click", async () => {
  if (!state.teamMode || !state.current || !state.capabilities.manage) return;
  state.memberProjectId = state.current.id; state.members = []; state.memberHasMore = false;
  $("members-project").textContent = state.current.name;
  $("member-create-details").hidden = !state.teamSession?.global_admin;
  $("members-dialog").showModal();
  empty($("members-list"), "正在载入成员…");
  try { await loadMembers(true); } catch (error) { empty($("members-list"), "成员清单暂不可用。"); toast(error.message); }
});
$("members-more").addEventListener("click", async () => {
  const button = $("members-more"); button.disabled = true;
  try { await loadMembers(); } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
$("member-assign-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.memberProjectId || state.memberProjectId !== state.current?.id) return;
  const button = event.target.querySelector("[type=submit]"); button.disabled = true;
  try {
    await request("/api/projects/" + state.memberProjectId + "/members", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("member-username").value, role: $("member-role").value }),
    });
    event.target.reset(); await loadMembers(true); toast("已加入当前项目");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
$("member-create-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.teamSession?.global_admin || !state.memberProjectId || state.memberProjectId !== state.current?.id) return;
  const projectId = state.memberProjectId;
  const button = event.target.querySelector("[type=submit]"); button.disabled = true;
  const username = $("new-member-username").value;
  const password = $("new-member-password").value;
  let created = false;
  try {
    await request("/api/team/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
    created = true;
    if (projectId !== state.current?.id) throw new Error("项目已切换，请在新项目重新授权");
    await request("/api/projects/" + projectId + "/members", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, role: $("new-member-role").value }),
    });
    event.target.reset(); await loadMembers(true); toast("团队账号已建立并加入当前项目");
  } catch (error) { toast(created ? "账号已建立，但项目授权未完成：" + error.message + "。请在已有账号处重试。" : error.message); }
  finally { $("new-member-password").value = ""; button.disabled = false; }
});
$("member-revoke-confirm").addEventListener("click", async () => {
  const target = state.memberRevoke;
  if (!target || target.projectId !== state.current?.id) return;
  const button = $("member-revoke-confirm"); button.disabled = true;
  try {
    await request("/api/projects/" + target.projectId + "/members/" + target.userId, { method: "DELETE" });
    $("member-revoke-dialog").close(); state.memberRevoke = null;
    await loadMembers(true); await refreshProject(); toast("已移除项目权限");
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
$("members-dialog").addEventListener("close", () => {
  $("member-create-form").reset(); $("member-assign-form").reset();
  $("member-create-details").open = false;
  state.members = []; state.memberHasMore = false; state.memberProjectId = null;
  if ($("member-revoke-dialog").open) $("member-revoke-dialog").close();
});
$("identity-status").addEventListener("click", async () => {
  if (!state.current) return;
  state.identityProjectId = state.current.id; state.identity = null;
  $("identity-project-name").textContent = state.current.name;
  $("identity-verify-form").reset(); $("identity-reopen-form").reset();
  const source = $("identity-source"); source.replaceChildren();
  const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "选择已解析的项目原件"; source.append(placeholder);
  for (const item of state.sources.filter((row) => row.kind === "document" && jobFor(row)?.status === "completed")) {
    const option = document.createElement("option"); option.value = item.id; option.textContent = item.reference; source.append(option);
  }
  $("identity-locator").replaceChildren();
  renderIdentityEvidenceType();
  $("identity-dialog").showModal(); renderIdentity();
  try { await loadIdentity(); } catch (error) { empty($("identity-current"), "主体记录暂不可用。"); toast(error.message); }
});
$("identity-evidence-type").addEventListener("change", renderIdentityEvidenceType);
$("identity-source").addEventListener("change", async () => {
  try { await loadIdentityLocators(); } catch (error) { toast(error.message); }
});
$("identity-verify-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = state.identityProjectId;
  if (!state.teamMode || !state.capabilities.manage || !projectId || projectId !== state.current?.id || !state.identity) return;
  const button = event.target.querySelector("[type=submit]"); button.disabled = true;
  const sourceMode = $("identity-evidence-type").value === "source";
  try {
    await request(`/api/projects/${projectId}/identity`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ legal_name: $("identity-legal-name").value,
        registration_code: $("identity-registration-code").value || null,
        evidence_source_id: sourceMode ? $("identity-source").value : null,
        evidence_locator: sourceMode ? $("identity-locator").value : null,
        evidence_url: sourceMode ? null : $("identity-evidence-url").value,
        note: $("identity-note").value, expected_version: identityVersion() }),
    });
    await loadIdentity(); await refreshProject(); toast("已记录人工主体核对");
  } catch (error) { toast(error.message); if (error.status === 409) await loadIdentity(); }
  finally { button.disabled = false; }
});
$("identity-reopen-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = state.identityProjectId;
  if (!state.teamMode || !state.capabilities.manage || !projectId || projectId !== state.current?.id || !state.identity) return;
  const button = event.target.querySelector("[type=submit]"); button.disabled = true;
  try {
    await request(`/api/projects/${projectId}/identity/reopen`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: $("identity-reopen-note").value, expected_version: identityVersion() }),
    });
    await loadIdentity(); await refreshProject(); toast("主体已恢复为未核验");
  } catch (error) { toast(error.message); if (error.status === 409) await loadIdentity(); }
  finally { button.disabled = false; }
});
$("identity-dialog").addEventListener("close", () => {
  state.identity = null; state.identityProjectId = null;
  $("identity-verify-form").reset(); $("identity-reopen-form").reset();
});
for (const id of ["new-project", "welcome-create"]) $(id).addEventListener("click", () => { $("create-dialog").showModal(); $("project-input").focus(); });
$("overview-public-monitoring").addEventListener("click", () => {
  state.view = "changes";
  renderView();
  schedulePoll();
  document.querySelector('.public-monitor-settings').open = true;
  $("public-monitor-title").scrollIntoView({ behavior: "smooth", block: "start" });
});
for (const id of ["header-upload", "material-upload"]) $(id).addEventListener("click", () => $("file-input").click());
$("material-search").addEventListener("input",applyMaterialFilter);
$("material-topic").addEventListener("change",applyMaterialFilter);
$("add-monitoring").addEventListener("click", () => { $("monitoring-dialog").showModal(); $("monitoring-url").focus(); });
$("start-public-search").addEventListener("click", async () => {
  if (!state.current) return;
  try {
    const path = `/api/projects/${state.current.id}/monitoring/public-search`;
    const [gdelt, brave] = await Promise.all([request(path), request(`${path}?provider=brave`)]);
    state.publicSearchPreviews = { gdelt, brave };
    const existingKinds = new Set(state.monitoringPlans.filter((plan) => plan.kind === "brave_news" || plan.target.includes("api.gdeltproject.org/api/v2/doc/doc")).map((plan) => plan.kind));
    $("public-search-provider").querySelector('[value="gdelt"]').disabled = !gdelt.eligible || existingKinds.has("rss");
    $("public-search-provider").querySelector('[value="brave"]').disabled = !brave.eligible || existingKinds.has("brave_news");
    if (![...$("public-search-provider").options].some((option) => !option.disabled)) { toast("当前没有可开启的公开来源。请检查项目名称或管理员配置。"); return; }
    $("public-search-provider").value = brave.eligible && !existingKinds.has("brave_news") ? "brave" : "gdelt";
    if ($("public-search-provider").selectedOptions[0]?.disabled) $("public-search-provider").value = "brave";
    $("public-search-name").textContent = gdelt.project_name;
    renderPublicSearchChoice();
    $("public-search-dialog").showModal();
  } catch (error) { toast(error.message); }
});
function renderPublicSearchChoice() {
  const preview = state.publicSearchPreviews?.[$("public-search-provider").value];
  if (!preview) return;
  $("public-search-disclosure").textContent = preview.disclosure;
  $("public-search-budget").textContent = preview.provider === "brave"
    ? `机构本月已用 ${preview.monthly_used} / ${preview.monthly_limit} 次；缺少 Key 或额度用尽时不可开启。`
    : "免费来源可能不可达或受限流；失败会保留回执，不会静默切换到付费来源。";
}
$("public-search-provider").addEventListener("change", renderPublicSearchChoice);
$("public-search-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.current) return;
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    await request(`/api/projects/${state.current.id}/monitoring/public-search`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider: $("public-search-provider").value }),
    });
    $("public-search-dialog").close(); await refreshProject(); toast("已开启试查；首轮任务在后台排队");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
$("refresh-monitoring").addEventListener("click", () => refreshProject());
$("competition-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.current) return;
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const data = new FormData(form);
    await request(`/api/projects/${state.current.id}/competitive-peers`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: String(data.get("name") || "").trim(), linked_project_id: data.get("linked_project_id") || null }),
    });
    form.reset(); await refreshProject(); toast("已加入候选；确认后才进入竞争集");
  } catch (error) { toast(error.message); }
  finally { submit.disabled = false; }
});
$("watch-create").addEventListener("click", openWatchCreate);
$("watch-create-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.current || !state.capabilities.propose) return;
  const projectId = state.current.id, submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
  try {
    await request(`/api/projects/${projectId}/watch-items`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ observation_id: $("watch-observation").value, title: $("watch-title").value.trim(), question: $("watch-question").value.trim(), owner: $("watch-owner").value.trim() || null, due_on: $("watch-due").value || null }),
    });
    $("watch-create-dialog").close(); event.target.reset(); await refreshProject(); toast("关注事项已提交，审核后才进入简报。");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
$("watch-review-source").addEventListener("click", () => {
  if (!state.watchReview) return;
  const source = state.sources.find((row) => row.id === state.watchReview.source_id && row.content_sha256 === state.watchReview.source_sha256);
  $("watch-review-dialog").close(); openSource(source, state.watchReview.source_locator);
});
$("watch-review-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.current || !state.watchReview || !state.capabilities.review) return;
  const projectId = state.current.id, item = state.watchReview;
  const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
  try {
    const body = { action: $("watch-review-action").value, expected_version: item.version, note: $("watch-review-note").value.trim() || null };
    if (!state.teamMode) body.reviewer = $("watch-reviewer").value.trim();
    await request(`/api/projects/${projectId}/watch-items/${item.id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("watch-review-dialog").close(); event.target.reset(); state.watchReview = null; await refreshProject(); toast("审核已记录，原件定位保留。");
  } catch (error) {
    if (error.status === 409 || error.status === 404) { $("watch-review-dialog").close(); state.watchReview = null; await refreshProject(); toast("事项已变化，请重新打开核对。"); }
    else toast(error.message);
  } finally { submit.disabled = false; }
});
$("watch-change-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.current || !state.watchChange || !state.capabilities.propose) return;
  const projectId = state.current.id, item = state.watchChange;
  const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
  try {
    await request(`/api/projects/${projectId}/watch-items/${item.id}/changes`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_version: item.version, target_status: $("watch-change-target").value, question: $("watch-change-question").value.trim(), owner: $("watch-change-owner").value.trim() || null, due_on: $("watch-change-due").value || null, evidence_observation_id: $("watch-change-evidence").value || null, reason: $("watch-change-reason").value.trim() }),
    });
    $("watch-change-dialog").close(); state.watchChange = null; await refreshProject(); toast("变更已提交，审核前不会改变当前简报。");
  } catch (error) {
    if (error.status === 409 || error.status === 404) { $("watch-change-dialog").close(); state.watchChange = null; await refreshProject(); toast("事项已变化，请重新打开。"); }
    else toast(error.message);
  } finally { submit.disabled = false; }
});
$("watch-change-review-source").addEventListener("click", () => {
  if (!state.watchChangeReview) return;
  const { item, change } = state.watchChangeReview;
  const observation = state.observations.find((row) => row.id === (change.evidence_observation_id || item.current_observation_id));
  const source = state.sources.find((row) => row.id === observation?.source_id);
  $("watch-change-review-dialog").close(); openSource(source, observation?.source_locator);
});
$("watch-change-review-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.current || !state.watchChangeReview || !state.capabilities.review) return;
  const projectId = state.current.id, { item, change } = state.watchChangeReview;
  const submit = event.target.querySelector('[type="submit"]'); submit.disabled = true;
  try {
    const body = { action: $("watch-change-review-action").value, expected_version: change.base_version, note: $("watch-change-review-note").value.trim() || null };
    if (!state.teamMode) body.reviewer = $("watch-change-reviewer").value.trim();
    await request(`/api/projects/${projectId}/watch-items/${item.id}/changes/${change.id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("watch-change-review-dialog").close(); state.watchChangeReview = null; await refreshProject(); toast("变更审核已记录。");
  } catch (error) {
    if (error.status === 409 || error.status === 404) { $("watch-change-review-dialog").close(); state.watchChangeReview = null; await refreshProject(); toast("变更已失效，请重新核对。"); }
    else toast(error.message);
  } finally { submit.disabled = false; }
});
$("run-coverage").addEventListener("click", async () => {
  const button = $("run-coverage"); button.disabled = true;
  try {
    state.coverageTask = await request(`/api/projects/${state.current.id}/tasks`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contract_version: 1, kind: "coverage", source_ids: state.sources.map((source) => source.id), idempotency_key: crypto.randomUUID() }),
    });
    renderCoverage();
  } catch (error) { toast(error.message); } finally { button.disabled = false; }
});
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => button.closest("dialog").close());
$("close-reader").addEventListener("click", () => {
  state.selectedSource = null; $("reader").hidden = true;
  const previous = readerReturn; readerReturn = null;
  if (previous?.projectId === state.current?.id && previous.view === state.view) previous.restore();
});
$("file-input").addEventListener("change", async (event) => { await uploadFiles([...event.target.files]); event.target.value = ""; });
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    const project = await request("/api/projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("project-input").value }) });
    state.projects.unshift(project); $("create-dialog").close(); event.target.reset(); state.view = "overview"; await selectProject(project.id); toast("项目已建立，主体尚未核验");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
$("monitoring-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.current) return;
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    await request(`/api/projects/${state.current.id}/monitoring/plans`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "rss", target: $("monitoring-url").value, cadence_minutes: Number($("monitoring-cadence").value) }),
    });
    $("monitoring-dialog").close(); event.target.reset(); await refreshProject(); toast("公开来源已添加，定时检查仍为关闭状态");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
async function submitObservationReview(action) {
  if (!state.review || !state.current || !$("review-form").reportValidity()) return;
  const buttons = [$("review-reject"), $("review-form").querySelector("[type=submit]")];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const body = { expected_version: state.review.version };
    if (!state.teamMode) body.reviewer = $("reviewer-input").value;
    await request(`/api/projects/${state.current.id}/observations/${state.review.id}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("review-dialog").close(); $("review-form").reset(); state.review = null; await refreshProject();
    toast(action === "reject" ? "候选已驳回，原件和审核记录保留" : (state.teamMode ? "候选事实已由当前账号确认" : "候选事实已确认（本机预览标识未认证）"));
  } catch (error) { toast(error.message); } finally { buttons.forEach((button) => { button.disabled = false; }); }
}
$("review-form").addEventListener("submit", (event) => {
  event.preventDefault(); submitObservationReview("confirm");
});
$("review-reject").addEventListener("click", () => submitObservationReview("reject"));
$("batch-review-open").addEventListener("click", openBatchReview);
$("batch-review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  if (!state.current || !state.capabilities.review || form.dataset.projectId !== state.current.id || !form.reportValidity()) return;
  const items = [...$("batch-review-list").querySelectorAll(".batch-review-item")].map((item) => ({
    observation_id: item.dataset.observationId,
    expected_version: Number(item.dataset.version),
    action: item.querySelector("select").value,
  }));
  if (!items.length || items.some((item) => !item.action)) return;
  const submit = form.querySelector("[type=submit]"); submit.disabled = true;
  try {
    const body = { items };
    if (!state.teamMode) body.reviewer = $("batch-reviewer-input").value.trim();
    await request(`/api/projects/${state.current.id}/observations/batch-review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("batch-review-dialog").close(); form.reset(); state.selectedCandidates.clear(); state.batchDecisions.clear();
    await refreshProject(); toast(`已核对 ${items.length} 条；原件和审核记录均保留`);
  } catch (error) {
    if (error.status === 409 || error.status === 404) {
      $("batch-review-dialog").close(); state.selectedCandidates.clear(); state.batchDecisions.clear(); await refreshProject();
      toast("候选已变化，整批未保存。请重新选择核对。");
    } else toast(error.message);
  } finally { submit.disabled = false; }
});
$("review-open-source").addEventListener("click", () => {
  if (!state.review) return;
  const source = state.sources.find((item) => item.id === state.review.source_id);
  const locator = state.review.source_locator;
  $("review-dialog").close(); openSource(source, locator);
});
$("metric-subject").addEventListener("change", syncCapitalMetricHint);
$("metric-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const draft = state.metricDraft;
  const source = state.sources.find((item) => item.id === draft?.sourceId);
  if (!draft || !source || state.current?.id !== draft.projectId || source.content_sha256 !== draft.sourceSha256 || !state.capabilities.propose) {
    toast("项目或来源版本已变化，请重新从原件定位记录。"); return;
  }
  const number = $("metric-number").value.trim();
  const period = $("metric-period").value.trim();
  if (!/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(number)) { toast("数值请填写有限的十进制数，不加千分位符号。"); return; }
  if (!/^(?:\d{4}|\d{4}-Q[1-4]|\d{4}-(?:0[1-9]|1[0-2]))$/.test(period)) { toast("期间请用 2025、2025-Q4 或 2025-12 的格式。"); return; }
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    await request(`/api/projects/${draft.projectId}/observations`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: draft.sourceId, subject: $("metric-subject").value, statement: $("metric-name").value.trim(), number, unit: $("metric-unit").value.trim(), period, scope: $("metric-scope").value.trim() || null, basis: $("metric-basis").value, source_locator: draft.locator }),
    });
    $("metric-dialog").close(); event.target.reset(); state.metricDraft = null;
    await refreshProject(); toast("指标候选已记录，等待人工核对。");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
$("holding-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const draft = state.holdingDraft;
  const source = state.sources.find((row) => row.id === draft?.sourceId);
  if (!draft || !source || state.current?.id !== draft.projectId || source.content_sha256 !== draft.sourceSha256 || !state.capabilities.propose) {
    toast("项目或来源版本已变化，请重新从原件定位记录。"); return;
  }
  const decimal = (id) => $(id).value.trim() || null;
  const cost = decimal("holding-cost"), pct = decimal("holding-pct"), value = decimal("holding-value");
  if (![cost, pct, value].some(Boolean)) { toast("请至少填写一项原件明确写出的持仓数值。"); return; }
  if ([cost, pct, value].some((entry) => entry && !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(entry))) { toast("数值请用十进制格式，不填千分位逗号。"); return; }
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    await request(`/api/projects/${draft.projectId}/holding-snapshots`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fund_name: $("holding-fund").value.trim(), as_of: $("holding-date").value,
        source_id: draft.sourceId, source_locator: draft.locator, invested_cost_wan: cost,
        ownership_pct: pct, current_value_wan: value }),
    });
    $("holding-dialog").close(); state.holdingDraft = null;
    await refreshProject(); toast("持仓候选已登记，需人工审核后才进入监控。");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
async function submitHoldingReview(action) {
  const item = state.holdingReview;
  if (!item || state.current?.id !== item.project_id || !state.capabilities.review) return;
  const reviewer = state.teamMode ? null : $("holding-reviewer").value.trim();
  if (!state.teamMode && !reviewer) { toast("请填写核对人标识。"); return; }
  try {
    await request(`/api/projects/${item.project_id}/holding-snapshots/${item.id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, expected_version: item.version, reviewer,
        note: $("holding-review-note").value.trim() || null }),
    });
    $("holding-review-dialog").close(); state.holdingReview = null;
    await refreshProject(); toast("持仓审核已记录。");
  } catch (error) {
    if (error.status === 409 || error.status === 404) { $("holding-review-dialog").close(); state.holdingReview = null; await refreshProject(); }
    toast(error.message);
  }
}
$("holding-review-form").addEventListener("submit", (event) => { event.preventDefault(); submitHoldingReview("confirm"); });
$("holding-review-reject").addEventListener("click", () => submitHoldingReview("reject"));
$("holding-review-open-source").addEventListener("click", () => {
  const item = state.holdingReview;
  if (!item) return;
  $("holding-review-dialog").close();
  openSource(state.sources.find((source) => source.id === item.source_id), item.source_locator);
});
$("impact-open-source").addEventListener("click", () => {
  if (!state.impactReview) return;
  const source = state.sources.find((item) => item.id === state.impactReview.trigger_source_id);
  $("impact-dialog").close();
  openSource(source);
});
$("impact-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.impactReview) return;
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    const body = { resolution: $("impact-resolution").value, note: $("impact-note").value };
    if (!state.teamMode) body.reviewer = $("impact-reviewer").value;
    await request(`/api/projects/${state.current.id}/review-impacts/${state.impactReview.id}/resolve`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("impact-dialog").close(); event.target.reset(); state.impactReview = null;
    await refreshProject(); toast("复核记录已保存，原判断未被改写");
  } catch (error) { toast(error.message); } finally { submit.disabled = false; }
});
let dragDepth = 0;
document.addEventListener("dragenter", (event) => { if ([...event.dataTransfer?.types || []].includes("Files")) { event.preventDefault(); dragDepth++; if (state.current && state.capabilities.propose) $("drop-overlay").hidden = false; } });
document.addEventListener("dragover", (event) => { if ([...event.dataTransfer?.types || []].includes("Files")) event.preventDefault(); });
document.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("drop-overlay").hidden = true; });
document.addEventListener("drop", (event) => { event.preventDefault(); dragDepth = 0; $("drop-overlay").hidden = true; if (event.dataTransfer?.files.length) uploadFiles([...event.dataTransfer.files]); });
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.target.querySelector("[type=submit]"); submit.disabled = true;
  try {
    const session = await request("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-username").value, password: $("login-password").value }),
    });
    state.teamSession = session;
    $("login-password").value = "";
    $("login-error").hidden = true;
    $("login-screen").hidden = true;
    applyMode();
    await loadProjects();
  } catch (error) {
    $("login-error").textContent = "登录失败，请检查账号或密码。";
    $("login-error").hidden = false;
  } finally { submit.disabled = false; }
});
$("logout-button").addEventListener("click", async () => {
  try { await request("/api/auth/logout", { method: "POST" }); } catch (error) { toast(error.message); return; }
  showLogin();
});
$("project-directory-search").addEventListener("input",renderProjects);
client = createClientShell({ state, request, toast, selectProject, renderProjects, renderView, loadReport, openSource, openReview, refreshProject,
  newProject: () => $("new-project").click(),
  closePanel: () => { state.selectedSource = null; $("reader").hidden = true; },
});
bootstrap().catch((error) => toast(`载入项目失败：${error.message}`));
