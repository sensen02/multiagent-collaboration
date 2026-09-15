/**
 * Unison Console —— 应用逻辑。
 * 服务端 SQLite/Runtime 是唯一事实源；这里只做投影、交互与轻量缓存。
 */

import { createClient, openStream, ApiError } from './api.js';
import { esc, attr, toast } from './ui.js';
import * as views from './views.js';

const api = createClient();

const state = {
  runs: [],
  tasks: [],
  models: [],
  questions: [],
  cursor: 0,
  data_dir: '',
  default_workspace: '',
  app_config: null,
  provider: null,
};

const view = {
  runId: null,
  tab: 'chat',
  filter: 'all',
  search: '',
  replayCursor: null,
  showAllStream: false,
  streamLimit: 120,
  eventFilter: '',
  knowledgeQuery: '',
  agentTask: null,
  jumpedQuestions: new Set(),
  compose: { runId: null, text: '' },
  drawer: null, // { kind: 'task' | 'blob' | 'skill', ... }
  lastArchive: null,
  refreshError: '',
};

const cache = new Map();
const MAX_EVENTS = 4000;

let viewEpoch = 0;
let refreshing = false;
let pendingRefresh = false;
let stream = null;

const $ = (selector) => document.querySelector(selector);

/* --------------------------------------------------------------- 缓存读取 */

function cached(key, loader) {
  if (cache.has(key)) return Promise.resolve(cache.get(key));
  const pending = loader().then((value) => {
    cache.set(key, value);
    return value;
  }).catch((error) => {
    cache.delete(key);
    throw error;
  });
  cache.set(key, pending);
  return pending;
}

function invalidate(prefix = '') {
  for (const key of [...cache.keys()]) {
    if (!prefix || String(key).startsWith(prefix)) cache.delete(key);
  }
}

const eventsOf = (runId) => cached(`events:${runId}`, () => api.get('events', { run_id: runId, limit: MAX_EVENTS }));
const filesOf = (runId) => cached(`files:${runId}`, () => api.get('files', { run_id: runId }));
const transcriptOf = (runId) => cached(`transcript:${runId}`, () => api.get('transcript', { run_id: runId, limit: 150 }));
const knowledgeOf = (runId, query) => cached(`knowledge:${runId}:${query || ''}`, () => api.get('knowledge', { run_id: runId, query: query || '' }));
const taskOf = (taskId) => cached(`task:${taskId}`, () => api.get('task', { id: taskId }));
const replayOf = (runId, until) => cached(`replay:${runId}:${until}`, () => api.get('replay', { run_id: runId, until }));
const providersOf = () => cached('providers', () => api.get('providers'));

/* ----------------------------------------------------------------- 选择器 */

const currentRun = () => state.runs.find((run) => run.id === view.runId) || null;
const currentTasks = () => {
  const run = currentRun();
  return run ? state.tasks.filter((task) => task.run_id === run.id) : [];
};
const openQuestions = () => {
  const run = currentRun();
  return run ? state.questions.filter((question) => question.run_id === run.id && question.status === 'open') : [];
};

/* -------------------------------------------------------------- 对话输入栏 */

/**
 * 对话栏发给谁：优先当前选中的 Agent；它若已被"修改目标"弃置（旧版本或 superseded），
 * 就落到当前版本的主调度——否则消息投进去没有任何东西会被唤醒，用户却以为说上话了。
 */
function composeTarget(run, agents) {
  const list = agents || [];
  const stale = (agent) => !agent || agent.status === 'cancelled' || agent.superseded === true
    || (agent.revision !== undefined && run.revision !== undefined && agent.revision !== run.revision);
  const selected = list.find((agent) => agent.id === view.agentTask) || null;
  if (!stale(selected)) return { target: selected, fallbackFrom: null };
  const root = list.find((agent) => !agent.parent_id && !stale(agent)) || null;
  return { target: root, fallbackFrom: root ? selected : null };
}

function composeHtml(run, agents) {
  const { target, fallbackFrom } = composeTarget(run, agents);
  const draft = view.compose.runId === run.id ? view.compose.text : '';
  return views.renderComposer({ run, target, fallbackFrom, draft, disabled: run.status === 'cancelled' });
}

/**
 * 草稿与焦点必须自己留住：SSE 每次有新事件都会重画整个主区（`host.innerHTML = …`），
 * 不保的话用户正在打的字会被下一次刷新抹掉，光标也会跳走。
 */
function composeFocusSnapshot() {
  const el = document.activeElement;
  if (!el || el.tagName !== 'TEXTAREA' || el.form?.dataset?.action !== 'compose') return null;
  return { start: el.selectionStart, end: el.selectionEnd };
}

function autoGrowComposer(el) {
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
}

function restoreComposer(snapshot) {
  const el = document.querySelector('form[data-action="compose"] textarea[name="prompt"]');
  if (!el) return;
  autoGrowComposer(el);
  if (!snapshot) return;
  el.focus();
  const max = el.value.length;
  const start = Math.min(snapshot.start ?? max, max);
  const end = Math.min(snapshot.end ?? start, max);
  try { el.setSelectionRange(start, end); } catch { /* 未聚焦时部分浏览器拒绝设置选区，忽略即可 */ }
}

/**
 * 对话页的聊天区是**自己滚动的**（输入栏固定在它下方），所以整区重画会把滚动位置打回顶部。
 * 每次重画前记下位置，重画后放回去；原本就贴在底部的人应当继续看到最新一条。
 */
function chatScrollSnapshot() {
  const el = document.querySelector('.chat-scroll');
  if (!el) return null;
  return { top: el.scrollTop, atBottom: el.scrollHeight - el.scrollTop - el.clientHeight < 48 };
}

function restoreChatScroll(snapshot) {
  const el = document.querySelector('.chat-scroll');
  if (!el || !snapshot) return;
  el.scrollTop = snapshot.atBottom ? el.scrollHeight : snapshot.top;
}

/**
 * 有 Agent 向人类提问时，自动跳到**那个 Agent 的对话页**。
 *
 * 为什么值得自动跳：提问会让整个运行停住（`human_ask` = 全体暂停），
 * 而此时人可能在别的任务、别的标签页上看别的东西——没有这一步，
 * "整个系统停着等人"这件事就只体现在状态里，得靠人自己发现。
 *
 * `jumpedQuestions` 记住已经跳过的提问，避免每次刷新都把用户拽回去；
 * 问题被回答/作废后自动从集合里移除，相同 id 不会再出现（问题 id 不复用）。
 */
function jumpToWaitingQuestion() {
  const open = state.questions.filter((question) => question.status === 'open');
  const openIds = new Set(open.map((question) => question.id));
  for (const id of [...view.jumpedQuestions]) if (!openIds.has(id)) view.jumpedQuestions.delete(id);
  const fresh = open.find((question) => !view.jumpedQuestions.has(question.id));
  for (const question of open) view.jumpedQuestions.add(question.id);
  if (!fresh) return;
  const run = state.runs.find((item) => item.id === fresh.run_id);
  if (run) view.runId = run.id;
  view.tab = 'chat';
  view.agentTask = fresh.task_id;
  toast('有 Agent 在等你回答：已跳到它的对话，整个运行已暂停', 'warn', 8000);
}

function visibleRuns() {  const query = view.search.trim().toLowerCase();
  const filter = views.FILTERS.find((item) => item.id === view.filter) || views.FILTERS[0];
  return state.runs
    .filter((run) => (filter.id === 'all' ? true : filter.match.includes(run.status)))
    .filter((run) => (query ? `${run.goal} ${run.model_id} ${run.id}`.toLowerCase().includes(query) : true))
    .sort((a, b) => b.created - a.created);
}

/* ------------------------------------------------------------------ 渲染 */

function renderChrome() {
  const run = currentRun();
  $('#run-list').innerHTML = views.renderRunList(visibleRuns(), { selected: view.runId, questions: state.questions });
  $('#run-filters').innerHTML = views.renderFilters(state.runs, view.filter);
  $('#sidebar-foot').innerHTML = views.renderSidebarFoot(state);
  $('#breadcrumb').innerHTML = views.renderBreadcrumb(run);
}

async function renderMain() {
  const epoch = ++viewEpoch;
  const host = $('#main');
  const run = currentRun();

  if (!run) {
    host.innerHTML = views.renderHero({ models: state.models, defaultWorkspace: state.default_workspace });
    return;
  }

  const counts = { graph: currentTasks().length || null };
  let body = '';

  try {
    if (view.tab === 'chat') {
      const data = await transcriptOf(run.id);
      if (epoch !== viewEpoch) return;
      const agents = data.agents || [];
      const openQuestions = data.open_questions || [];
      const holds = new Set(openQuestions.map((question) => question.task_id));
      // 谁会先被看到：有问题的 Agent 优先，其次主调度。这样"有人在等回答"的运行时，
      // 一进对话页就是那个正在等你的 Agent，而不是需要用户自己去找。
      if (!view.agentTask || !agents.some((agent) => agent.id === view.agentTask)) {
        const waiting = agents.find((agent) => holds.has(agent.id));
        view.agentTask = waiting ? waiting.id : (agents[0]?.id || null);
      }
      counts.chat = openQuestions.length || null;
      body = views.renderAgentTabs({ agents, selected: view.agentTask, questions: data.questions })
        + views.renderConversation({ run, agent: agents.find((agent) => agent.id === view.agentTask), agents, questions: data.questions, selected: view.agentTask, composer: composeHtml(run, agents) });
    } else if (view.tab === 'overview') {
      const [events, detail] = await Promise.all([
        eventsOf(run.id),
        run.root_task ? taskOf(run.root_task).catch(() => null) : Promise.resolve(null),
      ]);
      if (epoch !== viewEpoch) return;
      body = views.renderOverview({
        run,
        tasks: currentTasks(),
        questions: openQuestions(),
        reports: detail?.reports || [],
        events,
      });
    } else if (view.tab === 'graph') {
      const events = await eventsOf(run.id);
      if (epoch !== viewEpoch) return;
      const replay = view.replayCursor === null ? null : await replayOf(run.id, view.replayCursor);
      if (epoch !== viewEpoch) return;
      const range = [events[0]?.seq ?? 0, events.at(-1)?.seq ?? 0];
      counts.graph = currentTasks().length || null;
      body = views.renderGraph({ run, tasks: currentTasks(), replay, replayCursor: view.replayCursor, eventRange: range });
    } else if (view.tab === 'stream') {
      const events = await eventsOf(run.id);
      if (epoch !== viewEpoch) return;
      const relevant = events.filter((event) => event.run_id === run.id);
      counts.stream = relevant.length || null;
      body = views.renderTrajectory({ events: relevant, tasks: currentTasks(), limit: view.streamLimit, showAll: view.showAllStream });
    } else if (view.tab === 'files') {
      const data = await filesOf(run.id);
      if (epoch !== viewEpoch) return;
      counts.files = data.changes.length || null;
      body = views.renderFiles({ ...data, omitted: data.omitted_files || [] });
    } else if (view.tab === 'knowledge') {
      const items = await knowledgeOf(run.id, view.knowledgeQuery);
      if (epoch !== viewEpoch) return;
      counts.knowledge = items.length || null;
      body = views.renderKnowledge({ items, query: view.knowledgeQuery });
    } else if (view.tab === 'events') {
      const events = await eventsOf(run.id);
      if (epoch !== viewEpoch) return;
      const types = [...new Set(events.map((event) => event.type))].sort();
      body = views.renderEvents({ events, filter: view.eventFilter, types });
    }
  } catch (error) {
    if (epoch !== viewEpoch) return;
    body = `<div class="banner error"><div class="banner-main"><h4>加载失败</h4><div class="banner-body">${esc(error.message)}</div></div></div>`;
  }

  if (epoch !== viewEpoch) return;

  // 运行被人类问题挂起时，用一条自带解释的横幅代替普通问题列表：
  // 停的是全体 Agent，这个因果必须写在用户看得见的地方。
  const hold = views.renderHoldBanner({
    run,
    questions: openQuestions(),
    agents: currentTasks(),
    selected: view.agentTask,
  });
  const questionsHtml = hold ? '' : views.renderQuestions(openQuestions(), currentTasks());
  const archiveBanner = view.lastArchive
    ? `<div class="banner"><div class="banner-main"><h4>已归档</h4><div class="banner-body mono">${esc(view.lastArchive)}</div></div>
       <button class="btn sm" data-action="restore" data-path="${attr(view.lastArchive)}">恢复到新目录</button>
       <button class="btn sm" data-action="dismiss-archive">知道了</button></div>`
    : '';

  const focusSnapshot = view.tab === 'chat' ? composeFocusSnapshot() : null;
  const scrollSnapshot = view.tab === 'chat' ? chatScrollSnapshot() : null;
  host.innerHTML = `<div class="main-inner" data-tab="${attr(view.tab)}">
    ${views.renderRunHeader(run)}
    ${hold}
    ${archiveBanner}
    ${questionsHtml}
    ${views.renderTabs(view, counts)}
    <div id="tab-panel" role="tabpanel">${body}</div>
  </div>`;
  if (view.tab === 'chat') {
    restoreChatScroll(scrollSnapshot);
    restoreComposer(focusSnapshot);
  }
}

async function refresh() {
  if (refreshing) {
    pendingRefresh = true;
    return;
  }
  refreshing = true;
  try {
    do {
      pendingRefresh = false;
      try {
        const next = await api.get('state');
        Object.assign(state, next);
        if (typeof next.cursor === 'number') stream?.seed(next.cursor);
        if (!view.runId || !state.runs.some((run) => run.id === view.runId)) {
          view.runId = state.runs.length ? state.runs[state.runs.length - 1].id : null;
        }
        jumpToWaitingQuestion();
        setConnection('online');
        renderChrome();
        await renderMain();
        if (view.drawer) await renderDrawer();
      } catch (error) {
        setConnection('offline');
        const message = error instanceof ApiError ? error.message : String(error?.message || error);
        if (!state.runs.length) {
          $('#main').innerHTML = `<div class="main-inner"><div class="hero">
            <h1>无法连接本地服务</h1>
            <p>${esc(message)}</p>
            <div class="hero-actions"><button class="btn primary" data-action="retry">重新连接</button></div>
          </div></div>`;
        } else {
          toast(message, 'error');
        }
      }
    } while (pendingRefresh);
  } finally {
    refreshing = false;
  }
}

function setConnection(status) {
  const node = $('#conn');
  node.dataset.state = status;
  node.querySelector('.conn-text').textContent = status === 'online' ? '已连接' : status === 'connecting' ? '重连中' : '已断开';
}

/* ------------------------------------------------------------------ 抽屉 */

async function renderDrawer() {
  const host = $('#drawer');
  const drawer = view.drawer;
  if (!drawer) {
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  if (drawer.kind === 'blob') {
    host.innerHTML = views.renderBlob(drawer);
    return;
  }
  if (drawer.kind === 'skill') {
    host.innerHTML = views.renderSkillDetail(drawer);
    return;
  }
  try {
    const data = await taskOf(drawer.taskId);
    if (view.drawer !== drawer) return;
    host.innerHTML = views.renderDrawer({ ...data, run: currentRun() });
  } catch (error) {
    host.innerHTML = `<div class="drawer-head"><h2>加载失败</h2><button class="icon-btn" data-action="close-drawer" aria-label="关闭">✕</button></div>
      <div class="drawer-body"><div class="banner error"><div class="banner-main"><div class="banner-body">${esc(error.message)}</div></div></div></div>`;
  }
}

function openTask(taskId) {
  view.drawer = { kind: 'task', taskId };
  renderDrawer();
}

function openBlob(ref, label, offset = 0, existing = '') {
  const step = 20000;
  api.get('object', { ref, start: offset })
    .then((data) => {
      const content = existing + (data.content || '');
      view.drawer = { kind: 'blob', ref, label, content, total: data.total || content.length };
      renderDrawer();
    })
    .catch((error) => toast(error.message, 'error'));
}

/* -------------------------------------------------------------- 技能（skill） */

/** 技能按"项目根最近优先"发现，因此列表随当前运行的工作区变化。 */
function skillWorkspace() {
  const run = currentRun();
  return run?.workspace || state.default_workspace || '';
}

async function openSkillsDialog() {
  $('#dlg-skills').showModal();
  await renderSkillsDialog();
}

async function renderSkillsDialog() {
  const host = $('#skills-body');
  host.innerHTML = '<div class="note">正在扫描技能目录…</div>';
  try {
    const data = await api.get('skills', { workspace: skillWorkspace() });
    if (!$('#dlg-skills').open) return;
    host.innerHTML = views.renderSkills(data, skillWorkspace());
  } catch (error) {
    host.innerHTML = `<div class="banner error"><div class="banner-main"><h4>技能目录读取失败</h4>
      <div class="banner-body">${esc(error.message)}</div></div></div>`;
  }
}

async function openSkillDetail(name) {
  view.drawer = { kind: 'skill', name, loading: true };
  await renderDrawer();
  try {
    const data = await api.get('skill', { name, workspace: skillWorkspace() });
    view.drawer = { kind: 'skill', name, skill: data.skill, content: data.content };
  } catch (error) {
    view.drawer = { kind: 'skill', name, error: error.message || String(error) };
  }
  await renderDrawer();
}

/* ------------------------------------------------------------------ 动作 */

async function withBusy(button, action) {
  if (button?.disabled) return;
  try {
    if (button) button.disabled = true;
    await action();
  } catch (error) {
    toast(error.message || String(error), 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

const actions = {
  'select-run': (el) => {
    view.runId = el.dataset.run;
    view.replayCursor = null;
    view.showAllStream = false;
    view.eventFilter = '';
    view.agentTask = null;
    closeDrawer();
    renderChrome();
    renderMain();
  },

  'agent-tab': async (el) => {
    view.agentTask = el.dataset.task;
    // 从横幅或问题列表点进来时，也要落到对话页——跳过去却停在别的标签页等于没跳。
    view.tab = 'chat';
    await renderMain();
    $('#tab-panel')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  },

  filter: (el) => {
    view.filter = el.dataset.filter;
    renderChrome();
  },

  retry: () => refresh(),
  'new-run': () => openNewRunDialog(),
  'open-models': () => openModelsDialog(),

  'open-skills': () => openSkillsDialog(),

  'open-skill': (el) => openSkillDetail(el.dataset.skill),

  'reload-skills': (el) => withBusy(el, async () => {
    const data = await api.post('skills/reload', { workspace: skillWorkspace() });
    $('#skills-body').innerHTML = views.renderSkills(data, skillWorkspace());
    toast(`技能目录已重扫：${(data.skills || []).length} 个技能`, 'success');
  }),

  tab: (el) => {
    view.tab = el.dataset.tab;
    view.knowledgeQuery = '';
    renderMain();
  },

  'open-task': (el) => openTask(el.dataset.task),

  'close-drawer': () => closeDrawer(),

  'answer': (el) => withBusy(el, async () => {
    await api.post('answer', { id: el.dataset.question, answer: el.dataset.answer });
    invalidate();
    await refresh();
    toast('已回答，相关任务会恢复运行', 'success');
  }),

  'resume-task': (el) => withBusy(el, async () => {
    await api.post('resume', { task_id: view.drawer?.taskId });
    invalidate('task:');
    await refresh();
    toast('已继续该任务', 'success');
  }),

  'compact-task': (el) => withBusy(el, async () => {
    await api.post('compact', { task_id: view.drawer?.taskId });
    invalidate('task:');
    toast('已请求压缩：本轮工具结束后立即生效');
    await renderDrawer();
  }),

  'cancel-task': (el) => {
    const taskId = view.drawer?.taskId;
    if (!taskId || !confirm('取消该任务及其子树？已产生的文件与记录会保留。')) return;
    withBusy(el, async () => {
      await api.post('task/cancel', { task_id: taskId });
      invalidate();
      await refresh();
      toast('已取消任务', 'success');
    });
  },

  'revise': () => {
    const run = currentRun();
    if (!run) return;
    $('#form-revise').elements.goal.value = run.goal;
    hideError('#revise-error');
    $('#dlg-revise').showModal();
  },

  'toggle-pause': (el) => withBusy(el, async () => {
    const run = currentRun();
    if (!run) return;
    const path = run.status === 'paused' ? 'runs/resume' : 'runs/pause';
    await api.post(path, { run_id: run.id });
    invalidate();
    await refresh();
    toast(run.status === 'paused' ? '已继续运行' : '已暂停派发新任务', 'success');
  }),

  'archive': (el) => withBusy(el, async () => {
    const run = currentRun();
    if (!run) return;
    const result = await api.post('archive', { run_id: run.id });
    view.lastArchive = result.path;
    await renderMain();
    toast('已归档事件、文件版本与报告', 'success');
  }),

  'restore': (el) => withBusy(el, async () => {
    const result = await api.post('restore', { archive_path: el.dataset.path });
    toast(`已恢复到新目录：${result.workspace}`, 'success');
  }),

  'dismiss-archive': () => {
    view.lastArchive = null;
    renderMain();
  },

  'cancel': (el) => {
    const run = currentRun();
    if (!run || !confirm('停止后该运行不能再继续，确定停止？')) return;
    withBusy(el, async () => {
      await api.post('cancel', { run_id: run.id });
      invalidate();
      await refresh();
      toast('已停止该运行', 'success');
    });
  },

  'replay-live': () => {
    view.replayCursor = null;
    renderMain();
  },

  'stream-all': () => {
    view.showAllStream = true;
    renderMain();
  },

  'blob': (el) => openBlob(el.dataset.ref, el.dataset.label),
  'blob-more': (el) => {
    const drawer = view.drawer;
    if (drawer?.kind !== 'blob') return;
    openBlob(drawer.ref, drawer.label, Number(el.dataset.offset || 0), drawer.content);
  },

  'refresh-benchmarks': (el) => withBusy(el, async () => {
    const result = await api.post('benchmarks/refresh', {});
    invalidate('providers');
    view.refreshError = result.refresh_error || '';
    await renderModelsDialog();
    toast(result.refresh_error ? result.refresh_error : `已更新 ${result.matched} 个模型的评分（来源 ${result.source}）`, result.refresh_error ? 'warn' : 'success');
  }),

  'scan-provider': (el) => withBusy(el, async () => {
    const result = await api.post('providers/scan', { provider_id: el.dataset.provider });
    invalidate();
    await refresh();
    await renderModelsDialog();
    toast(`已从 ${el.dataset.provider} 扫描到 ${result.discovered} 个模型`, 'success');
  }),

  'verify-provider': (el) => withBusy(el, async () => {
    // 探测会消耗真实调用，所以限量：按"最久未验证"优先，每次最多 3 个，剩下的可以再点。
    const result = await api.post('providers/verify', { provider_id: el.dataset.provider, limit: 3 });
    invalidate();
    await refresh();
    await renderModelsDialog();
    const failed = result.checked - result.reachable.length;
    const skipped = (result.skipped || []).length;
    const parts = [`本次探测 ${result.checked} 个模型：${result.reachable.length} 个可用`];
    if (failed) parts.push(`${failed} 个失败`);
    if (skipped) parts.push(`跳过 ${skipped} 个（已有近期真实调用证据，或属于需人工处理的错误）`);
    if (!result.checked) parts.push('没有需要复验的模型');
    toast(parts.join('；'), failed ? 'warn' : 'success');
  }),

  'set-default-model': (el) => withBusy(el, async () => {
    const result = await api.post('providers/default', { provider_id: el.dataset.provider, model: el.dataset.model });
    invalidate();
    await refresh();
    await renderModelsDialog();
    toast(`主调度默认模型已设为 ${result.model_id}`, 'success');
  }),
};

function closeDrawer() {
  view.drawer = null;
  renderDrawer();
}

/* ------------------------------------------------------------------ 表单 */

function hideError(selector) {
  const node = $(selector);
  if (node) {
    node.hidden = true;
    node.textContent = '';
  }
}

function showError(selector, message) {
  const node = $(selector);
  if (node) {
    node.hidden = false;
    node.textContent = message;
  }
}

function openNewRunDialog() {
  const real = state.models.filter((model) => model.id !== 'offline-demo');
  const form = $('#form-new');
  form.reset();
  $('#new-model').innerHTML = views.sortModelsByBenchmark(real)
    .map((model) => `<option value="${attr(model.id)}">${esc(views.modelOptionLabel(model))}</option>`)
    .join('');
  const configuredDefault = state.app_config?.model_id;
  if (configuredDefault && real.some((model) => model.id === configuredDefault)) form.elements.model_id.value = configuredDefault;
  if (state.default_workspace) form.elements.workspace.value = state.default_workspace;
  showModelWarning();
  hideError('#new-error');
  $('#dlg-new').showModal();
  form.elements.goal.focus();
}

/** 默认模型不可用（例如供应商按账号组拒绝该模型）时，创建任务前就明确提示。 */
function showModelWarning() {
  const node = $('#new-model-warning');
  if (!node) return;
  const model = state.models.find((item) => item.id === $('#new-model').value);
  const message = model ? views.modelAvailabilityWarning(model) : '';
  node.textContent = message;
  node.hidden = !message;
}

/* --------------------------------------------------------- 模型 / Provider */

async function renderModelsDialog() {
  const host = $('#models-body');
  host.innerHTML = '<div class="note">正在读取 provider…</div>';
  try {
    const data = await providersOf();
    if (!$('#dlg-models').open) return;
    host.innerHTML = views.renderProviders({
      providers: data.providers || [],
      unmanaged: data.unmanaged || [],
      appConfig: data.app_config || null,
      importError: data.import_error || '',
      refreshError: view.refreshError,
    });
  } catch (error) {
    host.innerHTML = renderProviderLoadFailure(error);
  }
}

/** 前端资源随磁盘更新，Python 服务却在启动时定型；这里把版本不一致讲清楚。 */
function renderProviderLoadFailure(error) {
  const message = error?.message || String(error);
  if (/unknown endpoint/i.test(message)) {
    return `<div class="banner warn"><div class="banner-main">
      <h4>服务端尚未加载新接口</h4>
      <div class="banner-body">
        浏览器已使用新的控制台前端，但正在运行的服务进程仍是改动前启动的代码，因此没有 <code>/api/providers</code>。
        重启服务即可（队列、事件与文件版本都在 SQLite 中，不会丢失）：
        <pre class="code" style="margin-top:8px">python -m unison.server --port 8740</pre>
      </div>
    </div></div>`;
  }
  return `<div class="banner error"><div class="banner-main"><h4>读取 provider 失败</h4>
    <div class="banner-body">${esc(message)}</div></div></div>`;
}

function openModelsDialog() {
  view.refreshError = '';
  hideError('#import-error');
  $('#dlg-models').showModal();
  renderModelsDialog();
}

/* ------------------------------------------------------------------ 事件 */

document.addEventListener('click', (event) => {
  const closer = event.target.closest('[data-close]');
  if (closer) {
    closer.closest('dialog')?.close();
    return;
  }
  const el = event.target.closest('[data-action]');
  if (!el) return;
  const handler = actions[el.dataset.action];
  if (!handler) return;
  if (el.tagName === 'FORM') return; // 表单交由 submit 处理
  event.preventDefault();
  handler(el, event);
});

document.addEventListener('submit', (event) => {
  const form = event.target;
  const action = form.dataset.action;
  if (!action) return;
  event.preventDefault();

  if (action === 'answer-form') {
    const value = form.elements.answer.value.trim();
    if (!value) return;
    withBusy(form.querySelector('button'), async () => {
      await api.post('answer', { id: form.dataset.question, answer: value });
      invalidate();
      await refresh();
      toast('已回答，相关任务会恢复运行', 'success');
    });
    return;
  }

  if (action === 'send-message') {
    const value = form.elements.summary.value.trim();
    if (!value) return;
    withBusy(form.querySelector('button'), async () => {
      await api.post('message', { task_id: view.drawer?.taskId, summary: value });
      form.reset();
      invalidate('task:');
      await refresh();
      toast('消息已送达，等待中的任务会在下一批处理', 'success');
    });
    return;
  }

  if (action === 'compose') {
    const value = form.elements.prompt.value.trim();
    if (!value) return;
    const taskId = form.dataset.task;
    const runId = form.dataset.run;
    withBusy(form.querySelector('button[type="submit"]'), async () => {
      const run = state.runs.find((item) => item.id === runId);
      // 运行被暂停时，光投消息不会唤醒任何东西：先把运行放行，再说这句话。
      if (run && run.status === 'paused') await api.post('runs/resume', { run_id: runId });
      await api.post('message', { task_id: taskId, summary: value });
      view.compose = { runId, text: '' };
      form.elements.prompt.value = '';
      invalidate();
      await refresh();
      toast('已发送：模型会在当前轮结束后读到这条消息', 'success');
      restoreComposer({ start: 0, end: 0 });
    });
    return;
  }

  if (action === 'knowledge-search') {
    view.knowledgeQuery = form.elements.query.value.trim();
    invalidate(`knowledge:${view.runId}:`);
    renderMain();
  }
});

document.addEventListener('input', (event) => {
  const el = event.target;
  if (el?.name !== 'prompt' || el.form?.dataset?.action !== 'compose') return;
  view.compose = { runId: el.form.dataset.run || null, text: el.value };
  autoGrowComposer(el);
});

document.addEventListener('keydown', (event) => {
  // Enter 发送、Shift+Enter 换行。`isComposing` / keyCode 229 是中文输入法按回车选词的情况，
  // 不挡住它的话，用拼音打字的人每选一次词就会误发一条消息。
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  const el = event.target;
  if (el?.name !== 'prompt' || el.form?.dataset?.action !== 'compose') return;
  event.preventDefault();
  el.form.requestSubmit();
});

document.addEventListener('change', (event) => {
  const el = event.target.closest('[data-action]');
  if (!el) return;
  if (el.dataset.action === 'event-filter') {
    view.eventFilter = el.value;
    renderMain();
  }
  if (el.dataset.action === 'replay') {
    view.replayCursor = Number(el.value);
    renderMain();
  }
});

$('#new-run-btn').addEventListener('click', openNewRunDialog);
$('#run-search').addEventListener('input', (event) => {
  view.search = event.target.value;
  renderChrome();
});
$('#sidebar-toggle').addEventListener('click', (event) => {
  const sidebar = $('#sidebar');
  const open = sidebar.dataset.open !== 'true';
  sidebar.dataset.open = String(open);
  event.currentTarget.setAttribute('aria-expanded', String(open));
});
$('#models-btn').addEventListener('click', openModelsDialog);
$('#skills-btn').addEventListener('click', openSkillsDialog);

$('#dlg-new').addEventListener('close', () => hideError('#new-error'));
$('#new-model').addEventListener('change', () => showModelWarning());
$('#dlg-revise').addEventListener('close', () => hideError('#revise-error'));
$('#dlg-models').addEventListener('close', () => hideError('#import-error'));

$('#form-new').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form));
  if (!data.goal.trim()) return showError('#new-error', '目标不能为空');
  if (!data.workspace.trim()) return showError('#new-error', '项目目录不能为空');
  withBusy(form.querySelector('button[type="submit"]'), async () => {
    const run = await api.post('runs', { goal: data.goal.trim(), workspace: data.workspace.trim(), model_id: data.model_id });
    $('#dlg-new').close();
    view.runId = run.id;
    view.tab = 'overview';
    invalidate();
    await refresh();
    toast('任务已创建，主模型开始调度', 'success');
  });
});

$('#form-revise').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.target;
  const goal = form.elements.goal.value.trim();
  if (!goal) return showError('#revise-error', '新目标不能为空');
  withBusy(form.querySelector('button[type="submit"]'), async () => {
    await api.post('revise', { run_id: view.runId, goal });
    $('#dlg-revise').close();
    invalidate();
    await refresh();
    toast('已切换到新的目标版本，旧任务已停止', 'success');
  });
});

$('#form-import').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  const text = form.elements.config.value.trim();
  hideError('#import-error');
  if (!text) return showError('#import-error', '请粘贴 DSH provider 片段或 Codex TOML');
  button.disabled = true;
  try {
    const config = await api.post('providers/import', { config: text });
    form.elements.config.value = '';
    invalidate();
    await refresh();
    await renderModelsDialog();
    toast(`已导入 provider ${config.model_provider}，默认模型 ${config.model}`, 'success');
  } catch (error) {
    showError('#import-error', /unknown endpoint/i.test(error?.message || '')
      ? '服务端尚未加载新接口（当前进程仍是旧代码），请重启服务后重试'
      : (error.message || String(error)));
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && view.drawer) closeDrawer();
  if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
    event.preventDefault();
    $('#run-search').focus();
  }
});

/* ------------------------------------------------------------------ 主题 */

// 只跟随系统；控制台不再提供手动主题切换。
function applyTheme() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}

applyTheme();
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);

/* ------------------------------------------------------------ DSH 集成 */

if (document.modelContext?.registerTool) {
  const lifecycle = new AbortController();
  window.addEventListener('pagehide', () => lifecycle.abort(), { once: true });
  try {
    Promise.resolve(document.modelContext.registerTool({
      name: 'multiagent_create_task',
      description: '在 MultiAgent_Collaboration 控制台提交一个多模型协作任务并显示协作图',
      annotations: { readOnlyHint: false },
      inputSchema: {
        type: 'object',
        properties: { goal: { type: 'string' }, workspace: { type: 'string' }, model_id: { type: 'string' } },
        required: ['goal', 'workspace', 'model_id'],
        additionalProperties: false,
      },
      execute: async (args) => {
        if (!args || !['goal', 'workspace', 'model_id'].every((key) => typeof args[key] === 'string' && args[key].trim())) {
          throw new Error('需要目标、目录和模型');
        }
        const run = await api.post('runs', args);
        view.runId = run.id;
        view.tab = 'overview';
        invalidate();
        await refresh();
        return { run_id: run.id, status: run.status, workspace: run.workspace };
      },
    }, { signal: lifecycle.signal })).catch(() => {});
  } catch {
    /* 宿主不支持时忽略 */
  }
}

/* ------------------------------------------------------------------ 启动 */

await refresh();

// 事件流只推送最新游标；刷新做 800ms 合并，避免工具密集执行时反复重拉。
const TICK_INTERVAL = 800;
let lastRefreshAt = 0;
let tickTimer = null;

function scheduleRefresh() {
  const wait = Math.max(0, TICK_INTERVAL - (Date.now() - lastRefreshAt));
  clearTimeout(tickTimer);
  tickTimer = setTimeout(() => {
    lastRefreshAt = Date.now();
    invalidate();
    refresh();
  }, wait);
}

stream = openStream({
  onState: (status) => setConnection(status),
  onTick: () => scheduleRefresh(),
});
stream.seed(state.cursor || 0);

// 兜底：即使事件流断开，也保证界面最终一致。
setInterval(() => {
  if (document.hidden) return;
  lastRefreshAt = Date.now();
  invalidate();
  refresh();
}, 15000);
