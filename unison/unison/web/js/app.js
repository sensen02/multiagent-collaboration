/**
 * Unison Console —— 应用逻辑。
 * 服务端 SQLite/Runtime 是唯一事实源；这里只做投影、交互与轻量缓存。
 */

import { createClient, openStream, ApiError } from './api.js';
import { esc, attr, bytes, toast } from './ui.js';
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
  // 输入栏的瞬时状态：是否曾拿到焦点、是否正在输入法组词。
  // 组词期间**不重画**——重画会把输入法的候选状态连带没上屏的字一起丢掉。
  composerState: { focused: false, composing: false },
  drawer: null, // { kind: 'task' | 'blob' | 'skill', ... }
  lastArchive: null,
  archivedOpen: false,   // “已归档会话”默认折叠
  refreshError: '',
};

const cache = new Map();
const MAX_EVENTS = 4000;

let viewEpoch = 0;
let refreshing = false;
let pendingRefresh = false;
let stream = null;

const $ = (selector) => document.querySelector(selector);

/* ------------------------------------------------------------ 确认弹窗 */

/**
 * 破坏性操作的确认，用与应用内其它对话框同款的一个弹窗。
 *
 * 为什么不用原生 `confirm()`：它的标题栏显示的是页面地址，正文只能是一行纯文本，
 * 后果说不清楚；而且点"取消"没有任何反馈——用户不知道是没点上、还是被忽略了。
 * 这里把后果逐条列出来，确定与取消走同一条结算路径，不会留下悬挂的 await。
 */
let confirmResolve = null;

function settleConfirm(value) {
  const resolve = confirmResolve;
  confirmResolve = null;
  const dialog = $('#dlg-confirm');
  if (dialog.open) dialog.close();   // close 事件回来时 confirmResolve 已空，是安全的 no-op
  if (resolve) resolve(value);
}

function confirmAction({ title, lines = [], note = '', confirmLabel = '确定', cancelLabel = '取消' }) {
  const dialog = $('#dlg-confirm');
  if (confirmResolve) settleConfirm(false);   // 上一次没结算：先收掉，绝不叠弹窗
  $('#confirm-title').textContent = title;
  $('#confirm-body').innerHTML = (lines.length
    ? `<ul class="confirm-list">${lines.map((line) => `<li>${esc(line)}</li>`).join('')}</ul>`
    : '') + (note ? `<p class="note warn">${esc(note)}</p>` : '');
  $('#confirm-yes').textContent = confirmLabel;
  $('#confirm-no').textContent = cancelLabel;
  dialog.showModal();
  return new Promise((resolve) => { confirmResolve = resolve; });
}

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
/**
 * 重画前记下输入栏的状态。
 *
 * 三件事都要记，缺一件就会吞字：
 * - `text`：**以 DOM 为准**。草稿平时靠 `input` 事件写进 `view.compose`，但那是"上一次事件"
 *   的值；快照是紧挨着重画取的，DOM 才是用户此刻看到的真值。
 * - `focus`：以前只在"`document.activeElement` 正好是 textarea"时才返回快照，否则
 *   `restoreComposer` 直接 return、**不重新聚焦**——用户接着打的字全进了空气。
 *   现在改成"输入栏曾经拿到过焦点就记着"（`composerState.focused`），不依赖取样那一瞬。
 * - `start`/`end`：光标位置。
 */
function isComposerField(el) {
  return !!el && el.tagName === 'TEXTAREA' && el.form?.dataset?.action === 'compose';
}

function composeFocusSnapshot() {
  const el = document.querySelector('form[data-action="compose"] textarea[name="prompt"]');
  if (!el) return null;
  return {
    text: el.value,
    start: el.selectionStart,
    end: el.selectionEnd,
    focus: document.activeElement === el || view.composerState.focused,
  };
}

function autoGrowComposer(el) {
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
}

function restoreComposer(snapshot) {
  const el = document.querySelector('form[data-action="compose"] textarea[name="prompt"]');
  if (!el) return;
  if (snapshot?.text && el.value !== snapshot.text) {
    // DOM 比草稿新：以 DOM 为准写回，别让重画把最后几个字吞掉。
    el.value = snapshot.text;
    view.compose = { runId: el.form?.dataset?.run || null, text: snapshot.text };
  }
  autoGrowComposer(el);
  if (!snapshot?.focus) return;
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
 * 目标折叠：把占满屏幕的长目标收起来。
 *
 * 覆盖**两处**目标——页面顶部的**主目标**（`.run-title`）与当前 Agent 的目标（`.chat-goal`）。
 * 真机现场（`run_1da778c90c48`）：主目标正文 3465 字渲染成 **1830–2912px**，挂在所有标签页
 * **上方**；它一个人比整个可视区还高，往下滚几百像素看到的还是它。只折叠 `.chat-goal`
 * 完全没用——用户看到的是主目标。
 *
 * 三条规则：
 *
 * 1. **判"长"是测量出来的，不是字数**：超过 `GOAL_SHORT_LINES` 行才算长，折叠后显示
 *    `GOAL_COLLAPSED_LINES` 行。短目标不进这个机制，也不多出按钮。
 * 2. **触发看"被推出视野多少"**，而不是某个固定容器的 `scrollTop`。两个实测教训：
 *    对话页真正的滚动容器是 `main.main`（`.main { overflow-y: auto }`），而长目标下
 *    `.chat-scroll` 只有 115px、根本滚不动；而且判长要读 `scrollHeight`——它对**行内元素**
 *    不可靠（主目标在 `h1` 里就是个 span，因此被判成"短"、整块跳过，见 CSS 里的 `display:block`）。
 * 3. **折叠按被收起的高度补偿滚动位置**：主目标能收起 2800px，不补偿就是一次画面跳转。
 *    补偿后若已回到顶部就停在 0——用户要的正是"目标收起来，下面的内容立刻能看"。
 *
 * 用**滚动方向 + 时间护栏**决定何时展开，而不是位置：折叠的补偿会把 `scrollTop` 拉回 0，
 * 浏览器为此再发一次 scroll 事件，若"在顶部"就等于"展开"，它会在自己的补偿结果上反复横跳。
 * 现在：**向下滚 + 被推出视野 → 折叠；向上滚 + 已在顶部 + 距上次折叠超过 `GOAL_SETTLE_MS` → 展开**。
 * （早先版本用"记下自己造成的滚动位置"来识别回声，但那个一次性标记会被 SSE 重画复位，
 * 实测仍会自激；时间护栏不依赖任何会被重画清掉的东西。）
 *
 * 状态存模块级 Map（键是 `data-goal-key`）而不是 DOM：SSE 每有新事件就整区重画，
 * DOM 上的状态活不过一次刷新。
 */
const GOAL_SHORT_LINES = 3;
const GOAL_COLLAPSED_LINES = 2;
const GOAL_COLLAPSE_AT = 4;      // 目标顶部被推到滚动视口顶边之上多少像素才折叠
const GOAL_SETTLE_MS = 700;      // 折叠后多久内不接受"回到顶部就展开"（用于忽略自己的回声）
const goalStates = new Map();    // key → {collapsed, manual, collapsedAt}

function goalBlocks() {
  return [...document.querySelectorAll('[data-goal]')];
}

function goalStateOf(block) {
  const key = block.dataset.goalKey || 'goal';
  if (!goalStates.has(key)) goalStates.set(key, { collapsed: false, manual: false, collapsedAt: 0 });
  return goalStates.get(key);
}

/** 目标所属的滚动容器：往上找第一个**真的能滚**的祖先；都没有就退回页面本身。 */
function goalScroller(el) {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (/(auto|scroll|overlay)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) return node;
  }
  return document.scrollingElement || document.documentElement;
}

/** 目标顶部被推出滚动视口多少像素（负值表示还在视口顶边下方）。 */
function goalPushedOut(block) {
  const scroller = goalScroller(block);
  const viewTop = Math.max(0, scroller.getBoundingClientRect().top);
  return viewTop - block.getBoundingClientRect().top;
}

/** 套用折叠状态；`compensate` 为真时按被收起的高度补偿滚动位置。 */
function applyGoalState(block, collapsed, compensate = false) {
  const state = goalStateOf(block);
  const scroller = goalScroller(block);
  const before = block.getBoundingClientRect().height;
  const changed = state.collapsed !== collapsed;
  state.collapsed = collapsed;
  // 只在**状态真的变化**时打时间戳。每次重画都会把状态重新套一遍，
  // 若那时也刷新时间戳，"刚折叠"会永远成立，回顶展开就永远被护栏挡住（实测如此）。
  if (collapsed && changed) state.collapsedAt = Date.now();
  block.classList.toggle('is-collapsed', collapsed);
  const more = block.querySelector('.goal-more');
  if (more) more.hidden = !collapsed;
  const saved = before - block.getBoundingClientRect().height;
  // **有空间才补偿**。补偿是为了让"正在看的内容不动"；但如果收起的高度超过已滚距离，
  // 补偿会一路冲到顶部——那就把用户顶到顶上，而"在顶部"又意味着该展开（自相矛盾，
  // 而且实测会互相打架）。冲得到顶的时候就**不补偿**：让下面的内容顺势上移，
  // 用户往上滚回顶部时目标还会自己展开，两条行为都保住了。
  if (compensate && saved > 0) {
    const next = scroller.scrollTop - saved;
    if (next > 0) scroller.scrollTop = next;
  }
  return saved;
}

function onGoalScroll(event) {
  const scroller = event.target;
  if (!scroller || typeof scroller.scrollTop !== 'number') return;
  const top = scroller.scrollTop;
  const goingDown = top > (onGoalScroll.lastTop ?? 0);
  onGoalScroll.lastTop = top;
  for (const block of goalBlocks()) {
    if (block.dataset.long !== 'yes') continue;
    const state = goalStateOf(block);
    if (top <= 2) state.manual = false;               // 回到顶部即解除手动
    const settled = Date.now() - (state.collapsedAt || 0) > GOAL_SETTLE_MS;
    if (!goingDown && top <= 2 && settled) {          // 向上滚到顶 → 展开
      if (state.collapsed) applyGoalState(block, false);
      continue;
    }
    if (!goingDown || state.manual || state.collapsed) continue;
    if (goalPushedOut(block) < GOAL_COLLAPSE_AT) continue;
    applyGoalState(block, true, true);                // 向下滚且已被推出视野 → 折叠
  }
}

/**
 * 重画后重建折叠状态（每次 renderMain 都要跑）。
 *
 * 必须在 `restoreChatScroll()` **之前**调用：这里只按记住的状态套类、不做滚动补偿，
 * 让恢复滚动位置时面对的是最终布局，否则每次 SSE 刷新都会跳一下。
 * "长不长"要在这时候量——此刻元素是全新的、还没套过折叠类，高度就是完整内容高度。
 *
 * `entering` 表示刚切进对话页（接下来一定停在顶部）：清掉状态，与"滑到顶就展开"一致。
 */
function setupChatGoal(entering = false) {
  if (entering) goalStates.clear();
  onGoalScroll.lastTop = null;
  for (const block of goalBlocks()) {
    const text = block.querySelector('.goal-text');
    if (!text) continue;
    const line = parseFloat(getComputedStyle(text).lineHeight) || 20;
    block.dataset.long = text.scrollHeight > line * GOAL_SHORT_LINES + 1 ? 'yes' : 'no';
    if (block.dataset.long !== 'yes') {
      goalStateOf(block).collapsed = false;
      block.classList.remove('is-collapsed');
      const more = block.querySelector('.goal-more');
      if (more) more.hidden = true;
      continue;
    }
    applyGoalState(block, goalStateOf(block).collapsed);
  }
}

/**
 * 「往上划到头」= 向上滚的手势，而不是"滚动事件说位置回到了顶部"。
 *
 * 为什么必须听手势：折叠会让页面变短，浏览器会把 `scrollTop` 夹回 0（实测折叠后 17ms
 * 就来了一条 `top=0` 的 scroll 事件——护栏挡住了它），此后**页面已经没有可滚空间**，
 * 用户再怎么往上划也不会产生 scroll 事件，于是"回顶展开"永远等不到。
 * 手势是直接的意图，且在没有滚动空间时照样会发出来——这正是"划到头"的那一刻。
 */
function expandGoalsAtTop() {
  for (const block of goalBlocks()) {
    if (block.dataset.long !== 'yes') continue;
    const state = goalStateOf(block);
    if (!state.collapsed) continue;
    if (goalScroller(block).scrollTop > 2) continue;   // 还没到顶：先让滚动正常发生
    applyGoalState(block, false);
  }
}
document.addEventListener('wheel', (event) => { if (event.deltaY < 0) expandGoalsAtTop(); },
                          { capture: true, passive: true });

// 滚动监听挂在 document 的捕获阶段：滚动事件不冒泡，但捕获阶段能拿到**任意**容器的滚动，
// 因此不必每次重画都往新元素上重挂。它管**折叠**（往下滚）与键盘等非滚轮输入下的展开。
onGoalScroll.lastTop = null;
document.addEventListener('scroll', onGoalScroll, { capture: true, passive: true });

/**
 * 手动展开（点「展开目标」）。只做展开这一个方向，自动折叠仍归滚动管，
 * 因此不会出现"按钮说收起、滚动说展开"互相打架。
 */
function expandChatGoal(button) {
  const block = button?.closest('[data-goal]');
  if (!block) return;
  const state = goalStateOf(block);
  if (!state.collapsed) return;
  const scroller = goalScroller(block);
  const saved = applyGoalState(block, false);
  state.manual = true;
  if (saved < 0) scroller.scrollTop = Math.max(0, scroller.scrollTop - saved);   // 上方长高了，补回去
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

function matchesSearch(run, query) {
  if (!query) return true;
  return `${run.goal} ${run.model_id} ${run.id}`.toLowerCase().includes(query);
}

// 活动列表不含已归档会话：归档即结案，它挪到下面那个默认折叠的分区里。
// 状态筛选只作用于活动列表（归档会话已经不参与"进行中/需处理"的语义），搜索对两边都生效。
function visibleRuns() {
  const query = view.search.trim().toLowerCase();
  const filter = views.FILTERS.find((item) => item.id === view.filter) || views.FILTERS[0];
  return state.runs
    .filter((run) => !run.archived)
    .filter((run) => (filter.id === 'all' ? true : filter.match.includes(run.status)))
    .filter((run) => matchesSearch(run, query))
    .sort((a, b) => b.created - a.created);
}

// 已归档会话：记录一条都不少，默认按归档时间倒序。
function archivedRuns() {
  const query = view.search.trim().toLowerCase();
  return state.runs
    .filter((run) => run.archived)
    .filter((run) => matchesSearch(run, query))
    .sort((a, b) => (b.archived || 0) - (a.archived || 0));
}

/* ------------------------------------------------------------------ 渲染 */

function renderChrome() {
  const run = currentRun();
  $('#run-list').innerHTML = views.renderRunList(visibleRuns(), { selected: view.runId, questions: state.questions })
    + views.renderArchivedList(archivedRuns(), { selected: view.runId, questions: state.questions, open: view.archivedOpen });
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
    setupChatGoal(!scrollSnapshot);   // 先定布局（只套类、不滚动），再恢复滚动位置
    restoreChatScroll(scrollSnapshot);
    restoreComposer(focusSnapshot);
  }
}

async function refresh() {
  if (refreshing) {
    pendingRefresh = true;
    return;
  }
  // 输入法组词期间不重画：整区重画会把候选状态和还没上屏的字一起丢掉（"打着打着字就没了"）。
  // 组词结束后 `compositionend` 会补一次 refresh，所以这里推迟不会让界面停在旧状态。
  if (view.composerState?.composing) {
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
  'confirm-yes': () => settleConfirm(true),
  'confirm-no': () => settleConfirm(false),

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

  'goal-toggle': (el) => expandChatGoal(el),

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
    if (!taskId) return;
    withBusy(el, async () => {
      const ok = await confirmAction({
        title: '取消该任务及其子树',
        lines: [
          '取消这个任务，以及它派生的全部子任务',
          '已经产生的文件、事件与报告都保留',
          '正在执行的命令会被终止',
        ],
        confirmLabel: '取消任务',
      });
      if (!ok) { toast('已取消操作，任务照常运行', 'info'); return; }
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

  'toggle-archived': () => {
    view.archivedOpen = !view.archivedOpen;
    renderChrome();
  },

  'archive': (el) => {
    const run = currentRun();
    if (!run) return;
    withBusy(el, async () => {
      // 归档是结案，不是"再存一份"：先把后果逐条说清楚，再动手。
      const ok = await confirmAction({
        title: '归档并结案',
        lines: [
          '停止这个运行的全部 AI，以及它们派生的进程',
          '释放子任务工作区副本，磁盘回收',
          '事件、报告与文件修改记录保留，仍可查看与追溯',
          '归档后不能再继续这个运行',
        ],
        note: '归档不可撤销：副本删除后只能从归档恢复到新目录。',
        confirmLabel: '归档并结案',
      });
      if (!ok) { toast('已取消归档，什么都没变', 'info'); return; }
      const result = await api.post('archive', { run_id: run.id }, { timeoutMs: 900000 });
      view.lastArchive = result.archive_path;
      invalidate();
      await refresh();
      const parts = [];
      if (result.released?.length) parts.push(`释放 ${bytes(result.freed_bytes)} / ${result.released.length} 个工作区副本`);
      if (result.stopped?.length) parts.push(`停止 ${result.stopped.length} 个任务`);
      parts.push(`保留 ${result.files_kept} 条文件修改记录`);
      toast(`已归档并结案：${parts.join('，')}`, 'success', 8000);
    });
  },

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
    if (!run) return;
    withBusy(el, async () => {
      const ok = await confirmAction({
        title: '停止这个运行',
        lines: [
          '停止主调度与全部子任务，沿所有权树一起取消',
          '它们正在执行的命令会被终止',
          '已经写入项目的文件、事件与报告都保留',
          '停止后这个运行不能再继续；要接着做请新建任务',
        ],
        confirmLabel: '停止运行',
      });
      if (!ok) { toast('已取消停止，运行照常', 'info'); return; }
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

// 输入栏的焦点与输入法状态：重画要据此决定"要不要重新聚焦""能不能现在重画"。
document.addEventListener('focusin', (event) => {
  if (isComposerField(event.target)) view.composerState.focused = true;
});
document.addEventListener('focusout', (event) => {
  if (isComposerField(event.target)) view.composerState.focused = false;
});
document.addEventListener('compositionstart', (event) => {
  if (isComposerField(event.target)) view.composerState.composing = true;
});
document.addEventListener('compositionend', (event) => {
  if (!isComposerField(event.target)) return;
  view.composerState.composing = false;
  refresh();          // 组词期间被推迟的刷新，现在补上
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
// ✕、Esc、点遮罩都会走到这里：确认弹窗必须以"取消"收场，不能把 await 挂在那里。
$('#dlg-confirm').addEventListener('close', () => settleConfirm(false));
$('#dlg-confirm').addEventListener('cancel', (event) => { event.preventDefault(); settleConfirm(false); });

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
