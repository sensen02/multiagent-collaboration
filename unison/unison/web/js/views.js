/** 视图层：把运行时投影渲染成 HTML 字符串，不含副作用。 */

import {
  esc, attr, rich, jsonBlock, diffBlock, pill, quietPill, monoPill, statusOf,
  clock, stamp, relative, duration, truncate, taskTreeOf, isTerminal, VERDICT,
} from './ui.js';

/* ------------------------------------------------------------ 侧边栏 / 顶栏 */

export const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'active', label: '进行中', match: ['active', 'running', 'queued', 'waiting', 'paused'] },
  { id: 'completed', label: '已完成', match: ['completed'] },
  { id: 'failed', label: '需处理', match: ['failed'] },
  { id: 'closed', label: '已结束', match: ['cancelled', 'superseded'] },
];

export function renderFilters(runs, activeFilter) {
  return FILTERS.map((filter) => {
    const count = filter.id === 'all' ? runs.length : runs.filter((run) => filter.match.includes(run.status)).length;
    return `<button class="chip" data-action="filter" data-filter="${filter.id}" aria-pressed="${filter.id === activeFilter}">${esc(filter.label)}<span class="count">${count}</span></button>`;
  }).join('');
}

export function renderRunList(runs, { selected, questions }) {
  if (!runs.length) {
    return `<div class="empty" style="padding:32px 8px"><p>没有匹配的任务</p></div>`;
  }
  const openByRun = new Map();
  for (const question of questions) {
    if (question.status !== 'open') continue;
    openByRun.set(question.run_id, (openByRun.get(question.run_id) || 0) + 1);
  }
  return runs
    .map((run) => {
      const meta = statusOf(run.status);
      const open = openByRun.get(run.id) || 0;
      return `<button class="run-item" data-action="select-run" data-run="${attr(run.id)}" aria-current="${run.id === selected}">
        <span class="run-goal">${esc(run.goal)}</span>
        <span class="run-meta">
          <span class="pill st-${esc(meta.tone)}" style="height:18px;padding:0 6px"><i class="dot"></i>${esc(meta.label)}</span>
          <span>r${esc(run.revision)}</span>
          <span class="sep">·</span>
          <span>${esc(relative(run.created))}</span>
          ${open ? `<span class="pill st-waiting" style="height:18px;padding:0 6px">${open} 待答</span>` : ''}
        </span>
      </button>`;
    })
    .join('');
}

export function renderSidebarFoot(state) {
  return `<div>数据目录</div><div class="mono">${esc(state.data_dir || '—')}</div>`;
}

export function renderBreadcrumb(run) {
  if (!run) return `<span class="crumb">本地多模型协作台</span>`;
  return `<span class="crumb">${esc(truncate(run.goal, 70))}</span>`;
}

/* ------------------------------------------------------------------- 空状态 */

export function renderHero({ models, defaultWorkspace }) {
  const real = models.filter((model) => model.id !== 'offline-demo');
  const configured = real.filter((model) => model.configured);
  const step = (done, index, title, body) => `
    <div class="hero-step">
      <span class="n${done ? ' done' : ''}">${done ? '✓' : index}</span>
      <span><strong>${esc(title)}</strong><br><span class="muted">${esc(body)}</span></span>
    </div>`;
  return `<div class="main-inner"><div class="hero">
    <h1>让多个模型协作完成一件事</h1>
    <p>主模型负责拆解与调度，可以直接改代码，也可以递归委派子模型；等待、提问、文件版本和知识都由运行时记录。</p>
    <div class="hero-actions">
      <button class="btn primary" data-action="new-run">创建任务</button>
      <button class="btn" data-action="open-models">查看模型与 Provider</button>
    </div>
    <div class="hero-steps">
      ${step(configured.length > 0, 1, '接入一个模型', configured.length ? `已配置：${configured.map((m) => m.label || m.id).join('、')}` : '点击“查看模型与 Provider”导入 DSH YAML 或 Codex TOML 配置')}
      ${step(false, 2, '指定项目目录', defaultWorkspace ? `默认示例目录：${defaultWorkspace}` : '主模型会直接修改该目录')}
      ${step(false, 3, '提交目标并观察协作', '协作图、轨迹、文件差异和共享知识会实时更新')}
    </div>
  </div></div>`;
}

/* --------------------------------------------------------------- 运行头部 */

export function renderRunHeader(run) {
  return `<div class="run-header">
    <div class="run-header-main">
      <h1 class="run-title">${esc(run.goal)}</h1>
      <div class="run-meta">
        ${pill(run.status)}
        ${monoPill('r' + run.revision)}
        ${quietPill(run.model_id)}
        <span class="path" title="${attr(run.workspace)}">${esc(run.workspace)}</span>
        <span>创建于 ${esc(stamp(run.created))}</span>
        <span class="muted">·</span>
        <span class="mono tiny">${esc(run.id)}</span>
      </div>
    </div>
    <div class="run-actions">
      <button class="btn sm" data-action="revise"${run.status === 'cancelled' ? ' disabled' : ''}>修改目标</button>
      <button class="btn sm" data-action="toggle-pause">${run.status === 'paused' ? '继续运行' : '暂停'}</button>
      <button class="btn sm" data-action="archive">归档</button>
      <button class="btn sm danger" data-action="cancel">停止</button>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------- 问题 */

export function renderQuestions(questions, tasks) {
  if (!questions.length) return '';
  const byId = new Map(tasks.map((task) => [task.id, task]));
  return `<div class="card" style="margin-bottom:16px">
    <div class="card-head"><h3>等待你的回答</h3><span class="spacer"></span>${quietPill(questions.length + ' 个问题')}</div>
    <div class="card-body">
      ${questions
        .map((question) => {
          const task = byId.get(question.task_id);
          return `<div class="question-card">
            <div class="row wrap tiny muted">
              <span>${esc(task ? truncate(task.goal, 60) : question.task_id)}</span>
              <span class="mono">${esc(question.task_id)}</span>
            </div>
            <div class="q-text">${rich(question.question)}</div>
            ${question.options?.length ? `<div class="row wrap" style="margin-bottom:10px">${question.options.map((option) => `<button class="btn sm" data-action="answer" data-question="${attr(question.id)}" data-answer="${attr(option)}">${esc(option)}</button>`).join('')}</div>` : ''}
            <form class="q-form" data-action="answer-form" data-question="${attr(question.id)}">
              <input name="answer" placeholder="输入你的回答" required aria-label="回答该问题">
              <button class="btn primary" type="submit">回答并继续</button>
            </form>
          </div>`;
        })
        .join('')}
    </div>
  </div>`;
}

/* ------------------------------------------------------------------- 标签 */

export function renderTabs(view, counts) {
  const tabs = [
    ['chat', '对话'],
    ['overview', '总览'],
    ['graph', '协作图'],
    ['stream', '轨迹'],
    ['files', '文件'],
    ['knowledge', '知识'],
    ['events', '事件'],
  ];
  return `<div class="tabs" role="tablist" aria-label="运行视图">${tabs
    .map(([id, label]) => {
      const count = counts?.[id];
      // 对话页的计数是"有几个问题在等人"，所以它需要能被一眼看见，而不是一个灰数字。
      const attention = id === 'chat' && count ? ' attn' : '';
      return `<button class="tab${attention}" role="tab" aria-selected="${view.tab === id}" data-action="tab" data-tab="${id}">${esc(label)}${count ? `<span class="tab-count">${count}</span>` : ''}</button>`;
    })
    .join('')}</div>`;
}

/* ------------------------------------------------------------------- 总览 */

export function renderOverview({ run, tasks, questions, reports, events }) {
  const scoped = tasks.filter((task) => task.run_id === run.id && task.revision === run.revision);
  const counts = {
    total: scoped.length,
    running: scoped.filter((t) => t.status === 'running').length,
    queued: scoped.filter((t) => t.status === 'queued').length,
    waiting: scoped.filter((t) => t.status === 'waiting').length,
    completed: scoped.filter((t) => t.status === 'completed').length,
    failed: scoped.filter((t) => t.status === 'failed').length,
  };
  const models = [...new Set(scoped.map((task) => task.model_id))];
  const root = tasks.find((task) => task.id === run.root_task);
  const report = reports.find((item) => item.task_id === run.root_task);
  const recent = events.slice(-12).reverse();
  const lastEventAt = events.at(-1)?.created;
  const closed = !['active', 'paused', 'revising'].includes(run.status);

  const tile = (value, label, tone = '') => `<div class="stat${tone ? ' is-' + tone : ''}"><div class="stat-value">${value}</div><div class="stat-label">${esc(label)}</div></div>`;

  return `
    <div class="grid cols-4">
      ${tile(counts.total, '当前版本任务')}
      ${tile(counts.running, '执行中', counts.running ? 'accent' : '')}
      ${tile(counts.waiting + counts.queued, '等待 / 排队', counts.waiting ? 'warn' : '')}
      ${tile(counts.completed, '已完成', counts.completed ? 'success' : '')}
      ${tile(counts.failed, '需要处理', counts.failed ? 'error' : '')}
      ${tile(questions.length, '待回答问题', questions.length ? 'warn' : '')}
    </div>

    <div class="grid cols-2 section">
      <div class="card">
        <div class="card-head"><h3>运行信息</h3></div>
        <div class="card-body">
          <dl class="kv">
            <dt>状态</dt><dd>${pill(run.status)}</dd>
            <dt>目标版本</dt><dd>r${esc(run.revision)}</dd>
            <dt>主调度</dt><dd>${root ? esc(root.model_id) : esc(run.model_id)}</dd>
            <dt>参与模型</dt><dd>${models.map((model) => `<span class="pill quiet mono">${esc(model)}</span>`).join(' ') || '—'}</dd>
            <dt>已运行</dt><dd>${esc(duration(run.created, closed ? lastEventAt : null))}${closed ? '（已结束）' : ''}</dd>
            <dt>项目目录</dt><dd class="mono tiny">${esc(run.workspace)}</dd>
          </dl>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>任务分布</h3></div>
        <div class="card-body" style="display:flex;flex-direction:column;gap:10px">
          ${scoped.length ? scoped.slice(0, 10).map((task) => `
            <button class="row" data-action="open-task" data-task="${attr(task.id)}" style="background:none;border:0;padding:0;cursor:pointer;text-align:left;width:100%">
              <span style="flex:1;min-width:0"><span class="clamp-2" style="font-size:13px">${esc(truncate(task.goal, 80))}</span>
              <span class="tiny muted mono">${esc(task.model_id)}</span></span>
              ${pill(task.status)}
            </button>`).join('') : '<div class="empty"><p>还没有任务</p></div>'}
          ${scoped.length > 10 ? `<div class="tiny muted">另有 ${scoped.length - 10} 个任务，见协作图</div>` : ''}
        </div>
      </div>
    </div>

    ${report ? renderReport(report) : ''}

    <div class="card section">
      <div class="card-head"><h3>最近事件</h3><span class="spacer"></span><button class="btn sm" data-action="tab" data-tab="events">查看全部</button></div>
      <div class="card-body tight">
        ${recent.length ? recent.map((event) => `
          <div class="event-row">
            <span class="event-time">${esc(clock(event.created))}</span>
            <span><span class="event-name">${esc(event.type)}</span><div class="event-meta">${esc(event.task_id || '—')}</div></span>
          </div>`).join('') : '<div class="empty"><p>暂无事件</p></div>'}
      </div>
    </div>`;
}

export function renderReport(report) {
  const verdict = VERDICT[report.verification_status] || (report.verified_claim ? VERDICT.verified : VERDICT.not_verified);
  const toneClass = verdict.tone === 'success' ? 'st-completed' : verdict.tone === 'warn' ? 'st-waiting' : 'st-queued';
  const evidence = report.verification_evidence || null;
  return `<div class="card section">
    <div class="card-head"><h3>变更报告</h3><span class="spacer"></span><span class="pill ${toneClass}"><i class="dot"></i>${esc(verdict.label)}</span></div>
    <div class="card-body">
      ${evidence ? `<div class="note" style="margin-bottom:10px">
        <span class="pill st-queued"><i class="dot"></i>${esc(evidence.command_executions || 0)} 条命令证据</span>
        ${evidence.inspections ? `<span class="muted"> · ${esc(evidence.inspections)} 条目视/声明</span>` : ''}
        <span class="muted"> · 本任务共 ${esc(evidence.execution_records || 0)} 条执行记录。命令原文、退出码与是否超时都列在下面，够不够由人判断。</span>
      </div>` : ''}
      ${report.summary ? `<div class="entry-body" style="margin-bottom:12px">${rich(report.summary)}</div>` : ''}
      ${report.impact ? `<div class="note">影响范围：${esc(report.impact)}</div>` : ''}
      ${report.files?.length ? `<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:10px">
        <tbody>${report.files.map((file) => `<tr style="border-top:1px solid var(--c-line-1)">
          <td class="mono" style="padding:6px 8px 6px 0;word-break:break-all">${esc(file.path)}</td>
          <td class="muted" style="padding:6px 0">${esc(file.reason || '未提供原因')}</td>
        </tr>`).join('')}</tbody></table>` : '<div class="note">没有文件变更</div>'}
      ${report.omitted_files?.length ? `<div class="note warn" style="margin-top:10px">有 ${report.omitted_files.length} 个文件未纳入扫描：${esc(report.omitted_files.slice(0, 12).join('、'))}${report.omitted_files.length > 12 ? ' …' : ''}</div>` : ''}
      ${report.unknowns?.length ? `<div class="note warn" style="margin-top:6px">未确认：${esc(report.unknowns.join('；'))}</div>` : ''}
      ${report.unresolved?.length ? `<div class="note warn" style="margin-top:6px">未解决：${esc(report.unresolved.join('；'))}</div>` : ''}
      ${jsonBlock(report.execution_records || [], { label: '命令执行记录（含退出码与文件快照；不自动等同于测试通过）' })}
      ${jsonBlock(report.evidence, { label: `验证证据（${(report.evidence || []).length} 条）` })}
    </div>
  </div>`;
}

/* ----------------------------------------------------------------- 协作图 */

function taskCard(task, childCount) {
  const role = task.parent_id ? '子 AGENT' : '主调度';
  return `<button class="task-card" data-status="${attr(task.status)}" data-action="open-task" data-task="${attr(task.id)}">
    <span class="task-top">
      <span class="task-role">${esc(role)}</span>
      ${pill(task.status)}
      ${monoPill(task.model_id)}
      <span class="spacer" style="flex:1"></span>
      ${childCount ? `<span class="tiny muted">${childCount} 个子任务</span>` : ''}
    </span>
    <span class="task-goal">${esc(task.goal)}</span>
    <span class="task-foot">
      <span class="mono">${esc(task.id)}</span>
      <span>·</span>
      <span>${esc(relative(task.created))}</span>
      ${task.wait ? `<span>· ${esc(task.wait.note || '等待匹配事件')}</span>` : ''}
      ${task.dependencies?.length ? `<span>· 依赖 ${task.dependencies.length} 个任务</span>` : ''}
      ${task.epoch > 1 ? `<span>· 第 ${esc(task.epoch)} 次执行</span>` : ''}
    </span>
    ${task.error ? `<span class="task-error">${esc(task.error)}</span>` : ''}
  </button>`;
}

function treeHtml(task, byParent) {
  const children = byParent.get(task.id) || [];
  return `<div class="tree">
    ${taskCard(task, children.length)}
    ${children.length ? `<div class="tree-branch"><span class="tree-label">委派 ${children.length} 个子任务</span>${children.map((child) => treeHtml(child, byParent)).join('')}</div>` : ''}
  </div>`;
}

export function renderGraph({ run, tasks, replay, replayCursor, eventRange }) {
  if (replay) {
    const byParent = taskTreeOf(replay.tasks);
    const roots = replay.tasks.filter((task) => !task.parent_id);
    const revision = Math.max(1, ...replay.tasks.map((task) => task.revision));
    return `${replayBar(replayCursor, eventRange)}
      <div class="note" style="margin-bottom:12px">历史回放只重建任务与状态，用于观察协作过程；不重新执行工具，也不代表当时的完整文件与消息视图。</div>
      <div class="tree">${roots.filter((task) => task.revision === revision).map((task) => treeHtml(task, byParent)).join('')}</div>`;
  }
  const scoped = tasks.filter((task) => task.run_id === run.id);
  const byParent = taskTreeOf(scoped);
  const current = scoped.filter((task) => task.revision === run.revision);
  const currentTree = taskTreeOf(current);
  const roots = current.filter((task) => !task.parent_id);
  const history = scoped.filter((task) => task.revision !== run.revision);
  return `
    ${replayBar(replayCursor, eventRange)}
    <div class="tree">${roots.map((task) => treeHtml(task, currentTree)).join('') || '<div class="empty"><p>还没有任务</p></div>'}</div>
    ${history.length ? `<details class="raw" style="margin-top:16px"><summary>历史目标版本与已弃置任务（${history.length}）</summary>
      <div style="margin-top:10px;display:flex;flex-direction:column;gap:12px">
        ${[...new Set(history.map((task) => task.revision))].sort().map((revision) => `
          <div><div class="tiny muted" style="margin-bottom:6px">目标版本 r${revision}</div>
          ${treeHtml(history.find((task) => task.revision === revision && !task.parent_id) || history.find((task) => task.revision === revision), taskTreeOf(history.filter((task) => task.revision === revision)))}</div>`).join('')}
      </div>
    </details>` : ''}`;
}

function replayBar(replayCursor, eventRange) {
  const [min, max] = eventRange;
  return `<div class="replay-bar">
    <span>${replayCursor !== null && replayCursor !== undefined ? '历史回放' : '实时协作'}</span>
    <input type="range" min="${min}" max="${max}" value="${replayCursor ?? max}" data-action="replay" aria-label="事件时间轴">
    ${replayCursor !== null && replayCursor !== undefined ? `<button class="btn sm" data-action="replay-live">回到实时</button>` : ''}
    <span class="mono tiny">${esc(replayCursor ?? max)} / ${esc(max)}</span>
  </div>`;
}

/* ----------------------------------------------------------------- 轨迹 */

const TRAJECTORY_TYPES = new Set([
  'TaskSubmitted', 'MessageDelivered', 'QuestionRaised', 'HumanAnswered',
  'ModelReturned', 'ToolResult', 'ToolCalled',
  'TaskCompleted', 'TaskFailed', 'TaskCancelled', 'TaskSuperseded',
  'KnowledgePublished', 'ContextCompacted', 'ConflictDetected',
  'IntegrationCommitted', 'IntegrationAborted', 'GoalRevisionActivated',
  'WaitRegistered', 'TaskWoken',
]);

export function buildTrajectory(events, tasks) {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const results = new Map();
  for (const event of events) {
    if (event.type === 'ToolResult' && !results.has(event.payload.call_id)) results.set(event.payload.call_id, event);
  }
  const emittedCalls = new Set();
  const entries = [];

  const who = (taskId) => {
    const task = byId.get(taskId);
    if (!task) return { label: taskId || '系统', role: 'system', id: taskId };
    return {
      label: task.parent_id ? '子 AGENT' : '主调度',
      role: task.parent_id ? 'child' : 'root',
      model: task.model_id,
      id: task.id,
      goal: task.goal,
    };
  };

  for (const event of events) {
    if (!TRAJECTORY_TYPES.has(event.type)) continue;
    const payload = event.payload || {};
    if (event.type === 'ToolCalled') continue;

    if (event.type === 'ToolResult') {
      if (emittedCalls.has(payload.call_id)) continue;
      entries.push({ kind: 'tool', event, name: payload.name, args: null, result: payload.result, taskId: event.task_id });
      continue;
    }

    if (event.type === 'ModelReturned') {
      const message = payload.message || {};
      const calls = message.tool_calls || [];
      entries.push({
        kind: 'model',
        event,
        text: message.content || '',
        calls: calls.map((call) => ({ id: call.id, name: call.function?.name, args: call.function?.arguments })),
        usage: payload.usage,
        model: payload.model,
        taskId: event.task_id,
      });
      for (const call of calls) {
        emittedCalls.add(call.id);
        const result = results.get(call.id);
        entries.push({ kind: 'tool', event: result || event, name: call.function?.name, args: call.function?.arguments, result: result?.payload?.result, taskId: event.task_id });
      }
      continue;
    }

    if (event.type === 'MessageDelivered') {
      entries.push({ kind: 'message', event, payload, taskId: event.task_id });
      continue;
    }
    if (event.type === 'HumanAnswered') {
      entries.push({ kind: 'user', event, text: payload.answer, question: payload.question, taskId: event.task_id });
      continue;
    }
    if (event.type === 'QuestionRaised') {
      entries.push({ kind: 'question', event, text: payload.question, options: payload.options, taskId: event.task_id });
      continue;
    }
    if (event.type === 'TaskSubmitted') {
      entries.push({ kind: 'submit', event, text: payload.goal, model: payload.model, taskId: event.task_id });
      continue;
    }
    if (event.type === 'TaskCompleted') {
      entries.push({ kind: 'done', event, text: payload.summary, taskId: event.task_id });
      continue;
    }
    if (event.type === 'TaskFailed') {
      entries.push({ kind: 'error', event, text: payload.error, taskId: event.task_id });
      continue;
    }
    entries.push({ kind: 'system', event, text: event.type, payload, taskId: event.task_id });
  }
  return entries.map((entry) => ({ ...entry, who: who(entry.taskId) }));
}

function toolResultSummary(result) {
  if (result === undefined || result === null) return { text: '（结果未记录）', tone: '' };
  if (typeof result === 'object' && result.error) return { text: truncate(String(result.error), 400), tone: 'is-error' };
  if (typeof result === 'object') {
    const keys = Object.keys(result);
    if (result.exit_code !== undefined) return { text: `exit ${result.exit_code}${result.timed_out ? ' · 超时' : ''}`, tone: result.exit_code === 0 ? 'is-ok' : 'is-error' };
    if (result.written || result.edited) return { text: '已写入文件', tone: 'is-ok' };
    if (result.integrated !== undefined) return { text: result.integrated ? '集成成功' : '未集成（冲突或需重试）', tone: result.integrated ? 'is-ok' : 'is-error' };
    if (result.task_id) return { text: `创建/复用任务 ${result.task_id}`, tone: 'is-ok' };
    if (result.waiting !== undefined) return { text: result.waiting ? '已挂起等待' : '无需等待', tone: '' };
    if (result.completed) return { text: '已提交结果', tone: 'is-ok' };
    if (result.question_id) return { text: `提问 ${result.question_id}`, tone: '' };
    if (keys.length) return { text: keys.slice(0, 6).join(', '), tone: '' };
  }
  return { text: truncate(String(result), 300), tone: '' };
}

export function renderTrajectory({ events, tasks, limit, showAll }) {
  const all = buildTrajectory(events, tasks);
  if (!all.length) {
    return `<div class="card"><div class="empty"><h3>还没有协作记录</h3><p>模型调用、工具执行和任务间消息会按时间顺序显示在这里。</p></div></div>`;
  }
  const entries = showAll ? all : all.slice(-limit);
  const hidden = all.length - entries.length;
  return `<div class="stream">
    ${hidden > 0 ? `<button class="btn sm" data-action="stream-all" style="align-self:flex-start">显示更早的 ${hidden} 条记录</button>` : ''}
    ${entries.map(renderEntry).join('')}
  </div>`;
}

function renderEntry(entry) {
  const { kind, event, who } = entry;
  const head = `<div class="entry-head">
      <span class="who">${esc(who.label)}</span>
      ${who.model ? `<span class="mono">${esc(who.model)}</span>` : ''}
      <span>${esc(clock(event.created))}</span>
      <span>· #${esc(event.seq)}</span>
      ${who.goal ? `<span>· ${esc(truncate(who.goal, 46))}</span>` : ''}
    </div>`;

  if (kind === 'model') {
    const usage = entry.usage && Object.keys(entry.usage).length ? JSON.stringify(entry.usage) : '';
    return `<div class="entry" data-kind="model">
      <div class="entry-avatar">AI</div>
      <div class="entry-main">${head}
        <div class="entry-body">${entry.text ? rich(entry.text) : '<p class="muted">（本轮只发起工具调用）</p>'}</div>
        ${usage ? `<div class="tiny muted" style="margin-top:6px">usage ${esc(usage)}</div>` : ''}
        ${jsonBlock(entry.event.payload, { label: '模型原始返回' })}
      </div></div>`;
  }

  if (kind === 'tool') {
    const summary = toolResultSummary(entry.result);
    let args = entry.args;
    if (typeof args === 'string') {
      try { args = JSON.parse(args); } catch { /* 保留原始字符串 */ }
    }
    return `<div class="entry" data-kind="tool">
      <div class="entry-avatar">⚙</div>
      <div class="entry-main">${head}
        <div class="tool-line">
          <span class="tool-name">${esc(entry.name || 'unknown')}</span>
          <span class="tool-status ${summary.tone}">${esc(summary.text)}</span>
        </div>
        ${jsonBlock(args, { label: '调用参数' })}
        ${jsonBlock(entry.result, { label: '工具结果' })}
      </div></div>`;
  }

  if (kind === 'message') {
    const payload = entry.payload;
    const direction = payload.from_task
      ? `${truncate(payload.from_task, 14)} → ${truncate(payload.task_id, 14)}`
      : `系统 → ${truncate(payload.task_id, 14)}`;
    return `<div class="entry" data-kind="message">
      <div class="entry-avatar">✉</div>
      <div class="entry-main">${head}
        <div class="entry-body">${rich(payload.summary || '')}</div>
        <div class="tiny muted" style="margin-top:6px">${esc(direction)} · ${esc(payload.kind || 'message')}${payload.topic ? ' · ' + esc(payload.topic) : ''} · ${esc(payload.delivery || 'inbox')}</div>
      </div></div>`;
  }

  if (kind === 'user') {
    return `<div class="entry" data-kind="user">
      <div class="entry-avatar">你</div>
      <div class="entry-main">${head}
        <div class="entry-body">${rich(entry.text || '')}</div>
      </div></div>`;
  }

  if (kind === 'question') {
    return `<div class="entry" data-kind="question">
      <div class="entry-avatar">?</div>
      <div class="entry-main">${head}
        <div class="entry-body">${rich(entry.text || '')}</div>
        ${entry.options?.length ? `<div class="tiny muted" style="margin-top:6px">可选项：${esc(entry.options.join(' / '))}</div>` : ''}
      </div></div>`;
  }

  if (kind === 'submit') {
    return `<div class="entry" data-kind="system">
      <div class="entry-avatar">＋</div>
      <div class="entry-main">${head}
        <div class="entry-body"><p>${esc(entry.text || '')}</p></div>
        <div class="tiny muted" style="margin-top:6px">模型 ${esc(entry.model || '—')}</div>
      </div></div>`;
  }

  if (kind === 'done') {
    return `<div class="entry" data-kind="model">
      <div class="entry-avatar">✓</div>
      <div class="entry-main">${head}
        <div class="entry-body">${rich(entry.text || '已完成')}</div>
      </div></div>`;
  }

  if (kind === 'error') {
    return `<div class="entry" data-kind="error">
      <div class="entry-avatar">!</div>
      <div class="entry-main">${head}
        <div class="entry-body"><p>${esc(entry.text || '任务失败')}</p></div>
      </div></div>`;
  }

  const labels = {
    KnowledgePublished: '发布共享知识',
    ContextCompacted: '压缩工作上下文',
    CredentialsExposed: '读取模型凭据',
    ConflictDetected: '检测到集成冲突',
    IntegrationCommitted: '集成已提交',
    IntegrationAborted: '集成被中止',
    GoalRevisionActivated: '切换目标版本',
    WaitRegistered: '登记等待',
    TaskWoken: '等待被唤醒',
  };
  return `<div class="entry" data-kind="system">
    <div class="entry-avatar">·</div>
    <div class="entry-main">${head}
      <div class="entry-body"><p class="muted">${esc(labels[event.type] || event.type)}</p></div>
      ${jsonBlock(entry.payload, { label: '事件负载' })}
    </div></div>`;
}

/* ----------------------------------------------------------------- 对话 */

/**
 * 对话视图：一个 Agent 一页，内容是**它当时真正看到与说出的东西**
 * （服务端 `/api/transcript` 给的是事件日志派生出来的历史，不是另抄一份）。
 *
 * 与「轨迹」页的分工：轨迹是事件流，按事件类型铺开、带原始负载，用来查证；
 * 这里是对话，工具调用被翻成一句人话（"执行命令 npm test → 退出码 0"），
 * 用来读懂"这个 Agent 在干什么、卡在哪、要什么"。原始负载仍然收在折叠块里，一个字没丢。
 */

const TOOL_LABELS = {
  models_list: '查看可用模型',
  models_credentials: '读取模型凭据',
  tasks_list: '查看任务列表',
  tasks_submit: '委派子任务',
  tasks_yield: '挂起等待',
  agents_send: '给其它 Agent 发消息',
  tasks_close: '收束任务并排空请求',
  tasks_cancel: '取消任务',
  human_ask: '向人类提问',
  workspace_list: '列出目录',
  workspace_read: '读取文件',
  workspace_image_info: '读取图片信息',
  workspace_write: '写入文件',
  workspace_edit: '精确编辑文件',
  workspace_shell: '执行命令',
  workspace_diff: '查看文件差异',
  workspace_integrate: '集成子任务成果',
  workspace_archive: '归档本次运行',
  workspace_restore: '从归档恢复',
  artifacts_read: '读取工具原文',
  knowledge_search: '检索共享知识',
  knowledge_read: '读取共享信息',
  broadcast: '发布共享信息',
  skill: '加载技能',
  skills_list: '查看技能目录',
  usage_report: '查看用量与额度',
  context_compact: '压缩工作上下文',
  goals_activate_revision: '切换目标版本',
  tasks_complete: '提交结果',
};

function toolArgs(raw) {
  if (raw === undefined || raw === null || raw === '') return {};
  if (typeof raw === 'object') return raw;
  try {
    const value = JSON.parse(raw);
    return value && typeof value === 'object' ? value : { value };
  } catch {
    return { raw: String(raw) };
  }
}

/** 把调用参数压成一句"它在对谁做什么"，对话页读的就是这一行。 */
export function toolTarget(name, raw) {
  const args = toolArgs(raw);
  const text = (value, max = 120) => (value === undefined || value === null ? '' : truncate(String(value), max));
  if (args.error || args.raw) return text(args.error || args.raw, 160);
  switch (name) {
    case 'workspace_shell': return text(args.command, 200);
    case 'workspace_read':
    case 'workspace_write':
    case 'workspace_edit':
    case 'workspace_list':
    case 'workspace_diff':
    case 'workspace_image_info': return text(args.path, 120);
    case 'tasks_submit': return `${text(args.model_id || '继承本模型', 40)}：${text(args.goal, 100)}`;
    case 'agents_send': return `→ ${text(args.task_id, 24)}：${text(args.summary, 100)}`;
    case 'tasks_cancel':
    case 'tasks_close': return text(args.source_task || args.task_id, 40);
    case 'tasks_yield': {
      const ids = (args.task_ids || []).join('、');
      const topics = (args.topics || []).join('、');
      const until = args.timeout_seconds || args.max_wait_seconds;
      return [ids ? `任务 ${text(ids, 60)}` : '', topics ? `主题 ${text(topics, 60)}` : '',
        until ? `${until} 秒后到期` : '只被事件唤醒'].filter(Boolean).join(' · ') || '等待';
    }
    case 'human_ask': return text(args.question, 160);
    case 'knowledge_search': return text(args.query, 80) || `scope=${text(args.scope, 20)}`;
    case 'knowledge_read': return text(args.id, 40) || `scope=${text(args.scope || 'run', 20)} from_seq=${text(args.from_seq ?? 0, 10)}`;
    case 'broadcast': return text(args.title, 80) || text(args.content, 120);
    case 'skill': return `${text(args.name, 40)}${args.batch ? `（批量 ${args.batch.length} 份）` : ''}`;
    case 'workspace_integrate': return text(args.source_task, 40);
    case 'tasks_complete': return text(args.summary, 140);
    case 'models_credentials': return (args.model_ids || []).join('、') || '全部模型';
    case 'workspace_restore': return text(args.archive_path, 60);
    default: {
      const first = Object.values(args).find((value) => typeof value === 'string' && value.trim());
      return text(first, 120);
    }
  }
}

/** 结果同样翻成人话；原始 JSON 由 `jsonBlock` 收在折叠块里。 */
export function toolOutcome(result) {
  if (result === undefined || result === null) return { text: '没有记录结果', tone: '' };
  if (typeof result === 'string') return { text: truncate(result.trim() || '（空）', 300), tone: '' };
  if (typeof result !== 'object') return { text: String(result), tone: '' };
  if (Array.isArray(result)) return { text: `返回 ${result.length} 项`, tone: 'is-ok' };
  if (result.error) return { text: truncate(String(result.error), 300), tone: 'is-error' };
  if (result.question_id) return { text: '已提问 · 整个运行暂停，等人类回答', tone: 'is-wait' };
  if (result.exit_code !== undefined) {
    const tail = String(result.output || '').trim().split('\n').filter(Boolean).slice(-2).join(' / ');
    return {
      text: `退出码 ${result.exit_code}${result.timed_out ? '（超时被终止）' : ''}${tail ? ` · ${truncate(tail, 180)}` : ''}`,
      tone: result.exit_code === 0 ? 'is-ok' : 'is-error',
    };
  }
  if (result.written) return { text: `已写入 ${truncate(result.written, 120)}`, tone: 'is-ok' };
  if (result.edited) return { text: `已编辑 ${truncate(result.edited, 120)}`, tone: 'is-ok' };
  if (result.waiting !== undefined) return { text: result.waiting ? '已挂起，等事件唤醒' : '无需等待', tone: '' };
  if (result.completed) return { text: '已提交结果', tone: 'is-ok' };
  if (result.integrated !== undefined) {
    return { text: result.integrated ? '集成成功' : '未集成（冲突或需重试）', tone: result.integrated ? 'is-ok' : 'is-error' };
  }
  if (result.task_id) return { text: `子任务 ${truncate(result.task_id, 40)}`, tone: 'is-ok' };
  if (result.total_lines !== undefined) return { text: `读出 ${result.total_lines} 行中的第 ${result.start || 1} 行起`, tone: 'is-ok' };
  if (result.format) return { text: `${result.format}${result.width ? ` ${result.width}×${result.height}` : ''}${result.bytes ? ` · ${result.bytes} 字节` : ''}`, tone: 'is-ok' };
  if (result.entries) return { text: `返回 ${result.entries.length} 条`, tone: 'is-ok' };
  if (result.ok !== undefined) return { text: result.ok ? '执行成功' : truncate(String(result.error || result.message || '执行失败'), 200), tone: result.ok ? 'is-ok' : 'is-error' };
  const keys = Object.keys(result);
  if (!keys.length) return { text: '（空结果）', tone: '' };
  return { text: keys.slice(0, 5).join('、'), tone: '' };
}

/** 任务简报是 JSON；把它拆开显示，比让模型的一整段机器可读文本占满屏幕有用。 */
function parseBrief(content) {
  if (typeof content !== 'string' || !content.trim().startsWith('{')) return null;
  try {
    const data = JSON.parse(content);
    return data && typeof data === 'object' && data.goal && data.task_id ? data : null;
  } catch {
    return null;
  }
}

const INBOX_MARKER = '收件箱批次：';

function parseInbox(content) {
  if (typeof content !== 'string') return null;
  const at = content.indexOf(INBOX_MARKER);
  if (at < 0) return null;
  try {
    const items = JSON.parse(content.slice(at + INBOX_MARKER.length));
    return Array.isArray(items) ? items : null;
  } catch {
    return null;
  }
}

/**
 * 把一条条消息编成对话块：assistant 的工具调用与它的结果合并成一张卡片
 * （服务端历史里它们是两条消息，直接铺开会读成"调用一次、结果一次"两段不相干的东西）。
 */
export function buildConversation(messages = []) {
  const blocks = [];
  const calls = new Map();
  messages.forEach((message, index) => {
    if (message.role === 'system') {
      blocks.push({ kind: 'system', index, text: message.content || '' });
      return;
    }
    if (message.role === 'user') {
      const brief = parseBrief(message.content);
      if (brief && !blocks.some((block) => block.kind === 'brief')) {
        blocks.push({ kind: 'brief', index, brief, raw: message.content });
        return;
      }
      const inbox = parseInbox(message.content);
      if (inbox) {
        blocks.push({ kind: 'inbox', index, items: inbox });
        return;
      }
      blocks.push({ kind: 'user', index, text: message.content || '' });
      return;
    }
    if (message.role === 'assistant') {
      const block = { kind: 'assistant', index, text: message.content || '', reasoning: message.reasoning || '', calls: [] };
      for (const call of message.tool_calls || []) {
        const entry = { id: call.id, name: call.name, args: call.arguments, result: undefined };
        block.calls.push(entry);
        if (call.id) calls.set(call.id, entry);
      }
      blocks.push(block);
      return;
    }
    if (message.role === 'tool') {
      const entry = calls.get(message.tool_call_id);
      if (entry) entry.result = message.content;
      else blocks.push({ kind: 'orphan', index, text: message.content || '' });
    }
  });
  return blocks;
}

function toolCard(name, argsRaw, resultRaw) {
  const outcome = toolOutcome(resultRaw === undefined ? undefined : safeParse(resultRaw));
  const pending = resultRaw === undefined;
  return `<div class="tool-card" data-state="${pending ? 'pending' : (outcome.tone === 'is-error' ? 'error' : 'done')}">
    <div class="tool-head">
      <span class="tool-ico" aria-hidden="true">⚙</span>
      <span class="tool-label">${esc(TOOL_LABELS[name] || name || '未知工具')}</span>
      <code class="tool-target">${esc(toolTarget(name, argsRaw) || '—')}</code>
      <span class="tool-out ${outcome.tone}">${esc(pending ? '执行中…' : outcome.text)}</span>
    </div>
    ${jsonBlock(toolArgs(argsRaw), { label: '调用参数' })}
    ${resultRaw === undefined ? '' : jsonBlock(safeParse(resultRaw), { label: '原始结果' })}
  </div>`;
}

function safeParse(raw) {
  if (typeof raw !== 'string') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/** Agent 标签条：主调度在最前，其余按创建顺序；有未答问题时标红点。 */
export function renderAgentTabs({ agents, selected, questions }) {
  const open = new Map();
  for (const question of questions || []) {
    if (question.status === 'open') open.set(question.task_id, (open.get(question.task_id) || 0) + 1);
  }
  const rows = agents.map((agent, index) => {
    const role = agent.parent_id ? 'child' : 'root';
    const waiting = open.get(agent.id) || 0;
    const meta = statusOf(agent.status);
    const label = role === 'root' ? '主调度' : `子 AGENT ${index}`;
    return `<button class="agent-tab" role="tab" aria-selected="${agent.id === selected}" data-action="agent-tab" data-task="${attr(agent.id)}">
      <span class="agent-avatar" data-role="${role}">${role === 'root' ? '主' : '子'}</span>
      <span class="agent-tab-main">
        <span class="agent-tab-name">${esc(label)}${waiting ? `<span class="agent-badge" title="等人类回答">${waiting}</span>` : ''}</span>
        <span class="agent-tab-goal">${esc(truncate(agent.goal || '', 46))}</span>
      </span>
      <span class="agent-tab-state" data-tone="${waiting ? 'waiting' : meta.tone}">${esc(waiting ? '等你回答' : meta.label)}</span>
    </button>`;
  });
  return `<div class="agent-tabs" role="tablist" aria-label="Agent 对话">${rows.join('')}</div>`;
}

function questionCard(question, { compact = false } = {}) {
  const options = (question.options || []).length
    ? `<div class="row wrap" style="margin-bottom:10px">${question.options
        .map((option) => `<button class="btn sm" data-action="answer" data-question="${attr(question.id)}" data-answer="${attr(option)}">${esc(option)}</button>`)
        .join('')}</div>`
    : '';
  return `<div class="question-card${compact ? ' inline' : ''}">
    <div class="row wrap tiny muted">
      <span class="mono">${esc(question.id)}</span>
      <span>${esc(stamp(question.created))}</span>
    </div>
    <div class="q-text">${rich(question.question)}</div>
    ${options}
    <form class="q-form" data-action="answer-form" data-question="${attr(question.id)}">
      <input name="answer" placeholder="输入你的回答（回答后所有 Agent 继续）" required aria-label="回答该问题" autocomplete="off">
      <button class="btn primary" type="submit">回答并继续</button>
    </form>
  </div>`;
}

/**
 * 整个运行被人类问题挂起时的横幅：说清"停的是全体、等的是哪一句、回答后会发生什么"。
 * 这是自动暂停对用户唯一可见的地方，所以它必须自己解释清楚。
 */
export function renderHoldBanner({ run, questions, agents, selected }) {
  if (!run || run.status !== 'paused' || run.pause_reason !== 'human_question') return '';
  const list = questions || [];
  if (!list.length) return '';
  const byId = new Map((agents || []).map((agent) => [agent.id, agent]));
  return `<div class="hold-banner" role="status">
    <div class="hold-head">
      <span class="hold-dot" aria-hidden="true"></span>
      <div>
        <h4>所有 Agent 已暂停：等待人类回答</h4>
        <p class="tiny">${list.length} 个问题未答。回答后整个运行自动继续；也可以点右上角「继续运行」直接放行（问题会被跳过）。</p>
      </div>
    </div>
    ${list.map((question) => {
      const agent = byId.get(question.task_id);
      const role = agent ? (agent.parent_id ? '子 AGENT' : '主调度') : '未知 Agent';
      return `<div class="hold-item">
        <button class="hold-jump" data-action="agent-tab" data-task="${attr(question.task_id)}">${esc(role)} · ${esc(truncate(agent?.goal || question.task_id, 40))}${question.task_id === selected ? '（当前）' : ' →'}</button>
        <div class="q-text">${rich(question.question)}</div>
        <form class="q-form" data-action="answer-form" data-question="${attr(question.id)}">
          <input name="answer" placeholder="输入你的回答" required aria-label="回答该问题" autocomplete="off">
          <button class="btn primary" type="submit">回答并继续</button>
        </form>
      </div>`;
    }).join('')}
  </div>`;
}

/** 单个 Agent 的完整对话页。 */
export function renderConversation({ run, agent, agents, questions, selected, composer = '' }) {
  if (!agent) {
    return `<div class="card"><div class="empty"><h3>这个运行还没有可显示的 Agent</h3><p>创建任务后，主调度 Agent 的对话会出现在这里。</p></div></div>`;
  }
  const role = agent.parent_id ? 'child' : 'root';
  const openQuestions = (agent.questions || []).filter((question) => question.status === 'open');
  const answered = (agent.questions || []).filter((question) => question.status === 'answered');
  const blocks = buildConversation(agent.messages || []);
  const root = (agents || []).find((item) => !item.parent_id);

  const meta = [
    pill(agent.status),
    `<span class="mono tiny">${esc(agent.id)}</span>`,
    agent.model_id ? quietPill(agent.model_id) : '',
    role === 'child' && root ? `<span class="tiny muted">上级 ${esc(truncate(root.goal || '', 40))}</span>` : '',
    role === 'root' ? `<span class="path" title="${attr(run?.workspace || '')}">${esc(run?.workspace || '')}</span>` : '',
    `<span class="tiny muted">${blocks.filter((block) => block.kind === 'assistant').length} 轮模型回复 · 消息 ${agent.history_total ?? (agent.messages || []).length}</span>`,
  ].filter(Boolean).join('');

  const unread = (agent.unread || []).length;
  const truncated = agent.history_truncated
    ? `<div class="note tiny">为保持界面流畅，这里只显示最近 ${(agent.messages || []).length} 条消息（更早的 ${agent.history_truncated} 条仍在事件日志里，可用「轨迹」页查证）。</div>`
    : '';

  const waitNote = agent.wait
    ? `<div class="note tiny">等待条件：${esc((agent.wait.task_ids || []).join('、') || '—')} · 主题 ${esc((agent.wait.topics || []).join('、') || '—')} · ${esc(agent.wait.mode || 'any')}${agent.wait.deadline ? ` · ${esc(stamp(agent.wait.deadline))} 到期` : ' · 只被事件唤醒'}</div>`
    : '';

  const report = agent.report
    ? `<div class="card chat-report">
        <div class="card-head"><h3>${role === 'root' ? '最终报告' : '交付结果'}</h3><span class="spacer"></span>${agent.report.verification_status ? quietPill(VERDICT[agent.report.verification_status]?.label || agent.report.verification_status) : ''}</div>
        <div class="card-body">
          <div class="msg-body">${rich(agent.report.summary || agent.result || '')}</div>
          ${Object.keys(agent.report.files || {}).length ? `<div class="tiny muted" style="margin-top:8px">涉及文件：${esc(Object.keys(agent.report.files).join('、'))}</div>` : ''}
          ${(agent.report.unknowns || []).length ? `<div class="note warn tiny" style="margin-top:8px">未解决项：${esc(agent.report.unknowns.join('；'))}</div>` : ''}
        </div>
      </div>`
    : '';

  const errorNote = agent.error ? `<div class="banner error"><div class="banner-main"><h4>这个 Agent 出错了</h4><div class="banner-body">${esc(agent.error)}</div></div></div>` : '';

  return `<div class="chat">
    <div class="chat-scroll">
    <div class="chat-head">
      <span class="agent-avatar lg" data-role="${role}">${role === 'root' ? '主' : '子'}</span>
      <div class="chat-ident">
        <h3>${role === 'root' ? '主调度 Agent' : '子 AGENT'} <span class="tiny muted">${role === 'root' ? '（负责统筹与最终交付）' : '（独立工作副本，可继续委派）'}</span></h3>
        <div class="chat-meta">${meta}</div>
      </div>
    </div>
    <div class="chat-goal">${esc(agent.goal || '')}</div>
    ${unread ? `<div class="note tiny">还有 ${unread} 条消息没被这个 Agent 读到（下一轮会读到）。</div>` : ''}
    ${waitNote}
    ${errorNote}
    ${openQuestions.map((question) => questionCard(question, { compact: true })).join('')}
    <div class="chat-log">${blocks.length ? blocks.map(renderBlock).join('') : '<div class="empty"><p>这个 Agent 还没有开始说话。</p></div>'}</div>
    ${answered.length ? `<div class="chat-answered"><h4>已回答过的问题</h4>${answered.map((question) => `<div class="qa">
        <div class="q-text">${rich(question.question)}</div>
        <div class="a-text">你：${rich(question.answer || '')}</div>
      </div>`).join('')}</div>` : ''}
    ${truncated}
    ${report}
    </div>
    ${composer}
  </div>`;
}

/**
 * 对话输入栏：控制台里"给正在跑的系统补一句话"的入口。
 *
 * 它不引入任何新机制，只是把已有的消息协议摆到台面上：
 * `POST /api/message` → `deliver(delivery='wake')` → 消息进收件箱，
 * 当前这一轮（模型回复 + 它请求的工具批次）结束后被读到；等待中的任务立刻被唤醒；
 * 已交付但属于当前版本的任务会被重新排队继续同一条对话（不新建版本、不丢历史）。
 *
 * 因此"发送后什么时候被模型看到"是有确定答案的，hint 必须如实写出来——
 * 这一栏存在的意义就是替掉"只能靠「修改目标」才说得上话"。
 */
export function renderComposer({ run, target, fallbackFrom = null, draft = '', disabled = false }) {
  if (!run) return '';
  if (!target) {
    return `<div class="composer" role="group" aria-label="对话">
      <div class="composer-note warn">这个运行现在没有可接收消息的任务：任务都已结束、已取消，或只剩下旧目标版本的任务。用「修改目标」开新版本，或先「继续运行」。</div>
    </div>`;
  }
  const role = target.parent_id ? 'child' : 'root';
  const unread = (target.unread || []).length;
  const fallbackNote = fallbackFrom
    ? `<div class="composer-note">选中的 <span class="mono">${esc(fallbackFrom.id)}</span>（r${esc(fallbackFrom.revision ?? '?')}）已被目标切换弃置，发过去不会唤醒它；这条会发给当前版本的${role === 'root' ? '主调度' : '子 AGENT'}。</div>`
    : '';
  const meta = [
    `<span class="agent-avatar" data-role="${role}">${role === 'root' ? '主' : '子'}</span>`,
    `<span class="composer-who">发给${role === 'root' ? '主调度' : '子 AGENT'} <span class="mono tiny">${esc(target.id)}</span></span>`,
    target.model_id ? quietPill(target.model_id) : '',
    pill(target.status),
    unread ? `<span class="tiny muted">${unread} 条未读</span>` : '',
  ].filter(Boolean).join('');
  return `<form class="composer" data-action="compose" data-task="${attr(target.id)}" data-run="${attr(run.id)}">
    <div class="composer-head">${meta}</div>
    ${fallbackNote}
    <div class="composer-row">
      <textarea name="prompt" rows="1" ${disabled ? 'disabled' : ''}
        placeholder="${disabled ? '这个运行已取消，无法再交互' : '给这个 Agent 补一句话：新约束、纠正、放行……（Enter 发送，Shift+Enter 换行）'}"
        aria-label="发给该 Agent 的消息">${esc(draft)}</textarea>
      <button class="btn primary" type="submit" ${disabled ? 'disabled' : ''}>发送</button>
    </div>
    <div class="composer-hint tiny muted">${esc(composeHint(run, target))}</div>
  </form>`;
}

/** 把"这条消息什么时候会被模型读到"如实说清楚，而不是让人猜。 */
function composeHint(run, target) {
  if (run.status === 'cancelled') return '这个运行已取消：消息只会被记录，不会唤醒任何任务。';
  if (run.status === 'paused') {
    return run.pause_reason === 'human_question'
      ? '运行已暂停（在等人类回答）：发送会先恢复运行，模型在下一轮读到这条消息；未答的问题仍需要你回答或放行。'
      : '运行被手动暂停：发送会先恢复运行，模型在下一轮读到这条消息。';
  }
  switch (target.status) {
    case 'running': return '它正在跑这一轮：消息先入收件箱，等这一轮的模型回复与工具批次结束后被读到。';
    case 'waiting': return '它正卡在等待上：发送会立刻唤醒它，不再等原来的条件。';
    case 'queued': return '它在队列里：下一轮开始时读到这条消息。';
    case 'completed': return '它已交付过：发送会把它重新排队，在同一条对话里继续（不新建版本、不丢历史）。';
    case 'failed': return '它上次失败了：发送会先按「继续任务」恢复它，再投这条消息。';
    default: return '发送后模型会在本轮结束后读到这条消息。';
  }
}

function renderBlock(block) {
  if (block.kind === 'system') {
    return `<details class="raw msg-system"><summary>系统提示 · ${esc(String(block.text || '').length)} 字符（模型每次调用都会看到）</summary><pre class="code scroll">${esc(block.text || '')}</pre></details>`;
  }
  if (block.kind === 'brief') {
    const brief = block.brief;
    const knowledge = Array.isArray(brief.knowledge_index) ? brief.knowledge_index.length : 0;
    return `<div class="msg brief">
      <div class="msg-avatar">任</div>
      <div class="msg-body">
        <div class="msg-tag">任务简报</div>
        <div class="brief-goal">${esc(brief.goal || '')}</div>
        <div class="tiny muted">工作区 <span class="mono">${esc(brief.workspace || '')}</span> · 目标版本 r${esc(brief.goal_revision ?? 1)}${knowledge ? ` · 相关项目知识 ${knowledge} 条` : ''}${brief.review_model_id ? ` · 审查模型 ${esc(brief.review_model_id)}` : ''}</div>
        ${jsonBlock(brief, { label: '简报原文' })}
      </div></div>`;
  }
  if (block.kind === 'inbox') {
    return `<div class="msg inbox">
      <div class="msg-avatar">✉</div>
      <div class="msg-body">
        <div class="msg-tag">收到的消息（${block.items.length} 条）</div>
        ${block.items.map((item) => `<div class="inbox-item">
          <div class="inbox-head"><span class="mono tiny">${esc(item.from_task || '系统')}</span>
            <span class="tiny muted">${esc(item.kind || 'message')}${item.request_id ? ` · 请求 ${esc(item.request_id)}` : ''}${item.in_reply_to ? ` · 答复 ${esc(item.in_reply_to)}` : ''}${item.hop > 1 ? ` · 转发第 ${esc(item.hop)} 跳` : ''}</span></div>
          <div class="inbox-summary">${rich(item.summary || '')}</div>
        </div>`).join('')}
      </div></div>`;
  }
  if (block.kind === 'user') {
    return `<div class="msg user">
      <div class="msg-avatar">人</div>
      <div class="msg-body"><div class="msg-tag">人类介入</div>${rich(block.text)}</div></div>`;
  }
  if (block.kind === 'assistant') {
    return `<div class="msg assistant">
      <div class="msg-avatar">AI</div>
      <div class="msg-body">
        ${block.text ? rich(block.text) : ''}
        ${!block.text && !block.calls.length ? '<p class="muted">（空回复）</p>' : ''}
        ${block.reasoning ? `<details class="raw"><summary>思考过程</summary><pre class="code scroll">${esc(block.reasoning)}</pre></details>` : ''}
        ${block.calls.map((call) => toolCard(call.name, call.args, call.result)).join('')}
      </div></div>`;
  }
  if (block.kind === 'orphan') {
    return `<div class="msg tool-orphan"><div class="msg-avatar">⚙</div><div class="msg-body"><div class="msg-tag">无对应调用的工具结果</div>${jsonBlock(safeParse(block.text), { label: '原始结果' })}</div></div>`;
  }
  return '';
}

/* ----------------------------------------------------------------- 文件 */
export function renderFiles({ changes, history, conflicts, omitted }) {
  const openConflicts = (conflicts || []).filter((conflict) => conflict.status === 'open');
  return `
    ${openConflicts.length ? `<div class="banner error"><div class="banner-main">
      <h4>${openConflicts.length} 个集成冲突待处理</h4>
      <div class="banner-body">${openConflicts.map((conflict) => `${esc(conflict.id)}：${esc((conflict.files || []).map((f) => f.path).join('、'))}`).join('<br>')}</div>
    </div></div>` : ''}
    ${omitted?.length ? `<div class="banner warn"><div class="banner-main">
      <h4>${omitted.length} 个文件未纳入扫描</h4>
      <div class="banner-body">符号链接、超过 16 MiB 或被排除的目录不会记录版本。${esc(omitted.slice(0, 6).map((item) => (typeof item === 'string' ? item : `${item.task_id} · ${item.path}`)).join('、'))}${omitted.length > 6 ? ' …' : ''}</div>
    </div></div>` : ''}
    <div class="section-title">当前差异（相对任务基线）</div>
    ${changes.length ? changes.map((change) => `
      <details class="file-row">
        <summary>
          <span class="file-path">${esc(change.path)}</span>
          <span class="file-meta">${esc(change.task_id)} · r${esc(change.revision)}</span>
        </summary>
        <div class="file-body">${change.diff ? diffBlock(change.diff) : '<div class="empty"><p>二进制或空差异</p></div>'}</div>
      </details>`).join('') : '<div class="card"><div class="empty"><p>暂时没有文件差异</p></div></div>'}

    <div class="section">
      <div class="section-title">文件版本历史（${history.length}）</div>
      ${history.length ? `<div class="card"><div class="card-body tight">${history.slice().reverse().map((record) => `
        <div class="event-row">
          <span class="event-time">${esc(clock(record.created))}</span>
          <span>
            <span class="file-path mono">${esc(record.path)}</span>
            <div class="event-meta">${esc(record.actor || 'unknown')}${record.call_id ? ' · ' + esc(record.call_id) : ''} · r${esc(record.revision)}</div>
            <div class="row wrap" style="margin-top:4px">
              ${record.before ? `<button class="btn sm" data-action="blob" data-ref="${attr(record.before)}" data-label="修改前 · ${attr(record.path)}">修改前</button>` : '<span class="tiny muted">新增文件</span>'}
              ${record.after ? `<button class="btn sm" data-action="blob" data-ref="${attr(record.after)}" data-label="修改后 · ${attr(record.path)}">修改后</button>` : '<span class="tiny muted">已删除</span>'}
            </div>
          </span>
        </div>`).join('')}</div></div>` : '<div class="card"><div class="empty"><p>还没有记录到文件版本变化</p></div></div>'}
    </div>`;
}

/* ----------------------------------------------------------------- 知识 */

export function renderKnowledge({ items, query }) {
  return `
    <form class="search-row" data-action="knowledge-search">
      <input name="query" value="${attr(query)}" placeholder="搜索项目知识、结论或修改原因" aria-label="搜索共享知识">
      <button class="btn" type="submit">搜索</button>
    </form>
    ${items.length ? `<div class="card">${items.map((item) => `
      <div class="k-item">
        <h4>${esc(item.title)} ${item.stale ? '<span class="pill st-waiting"><i class="dot"></i>来源待刷新</span>' : '<span class="pill st-completed"><i class="dot"></i>有效</span>'}</h4>
        <div class="k-body">${esc(truncate(item.content, 900))}</div>
        <div class="k-foot">
          <span class="mono">${esc(item.id)}</span>
          <span>· r${esc(item.revision)}</span>
          <span>· ${esc((item.sources || []).map((source) => source.path).join('、') || '无文件来源')}</span>
          ${item.stale_reason ? `<span class="note warn">· ${esc(item.stale_reason)}</span>` : ''}
          ${item.goal_specific ? '<span>· 目标相关知识</span>' : ''}
        </div>
      </div>`).join('')}</div>` : `<div class="card"><div class="empty"><h3>没有匹配的知识</h3><p>Agent 可以在工作中发布带来源的项目结论，之后其他模型可直接复用。</p></div></div>`}`;
}

/* ----------------------------------------------------------------- 事件 */

export function renderEvents({ events, filter, types }) {
  const filtered = filter ? events.filter((event) => event.type === filter) : events;
  const recent = filtered.slice(-400).reverse();
  return `
    <div class="row wrap" style="margin-bottom:12px">
      <span class="tiny muted">共 ${events.length} 条事件，显示最近 ${recent.length} 条</span>
      <span class="spacer"></span>
      <select class="btn sm" data-action="event-filter" aria-label="按事件类型筛选" style="height:28px">
        <option value="">全部类型</option>
        ${types.map((type) => `<option value="${attr(type)}"${type === filter ? ' selected' : ''}>${esc(type)}</option>`).join('')}
      </select>
    </div>
    <div class="card"><div class="card-body tight">
      ${recent.length ? recent.map((event) => `
        <div class="event-row">
          <span class="event-time">${esc(clock(event.created))}</span>
          <span>
            <span class="event-name">${esc(event.type)}</span>
            <div class="event-meta">#${esc(event.seq)} · ${esc(event.task_id || '—')} · r${esc(event.revision ?? '—')}</div>
            ${jsonBlock(event.payload, { label: '事件负载' })}
          </span>
        </div>`).join('') : '<div class="empty"><p>没有匹配的事件</p></div>'}
    </div></div>`;
}

/* ----------------------------------------------------------------- 抽屉 */

export function renderDrawer({ task, messages, reports, changes, events, run }) {
  const pending = (messages || []).filter((message) => !message.consumed);
  return `
    <div class="drawer-head">
      <h2>${esc(task.goal)}</h2>
      <button class="icon-btn" data-action="close-drawer" aria-label="关闭详情">✕</button>
    </div>
    <div class="drawer-body">
      <div class="row wrap">
        ${pill(task.status)}
        ${monoPill(task.model_id)}
        ${task.superseded ? '<span class="pill st-superseded">已弃置</span>' : ''}
        <span class="spacer"></span>
        ${task.epoch > 1 ? `<span class="tiny muted">第 ${esc(task.epoch)} 次执行</span>` : ''}
      </div>

      ${task.error ? `<div class="banner error"><div class="banner-main"><h4>执行错误</h4><div class="banner-body">${esc(task.error)}</div></div></div>` : ''}

      <div class="drawer-section">
        <dl class="kv">
          <dt>任务 ID</dt><dd class="mono">${esc(task.id)}</dd>
          <dt>父任务</dt><dd class="mono">${task.parent_id ? `<button class="btn sm" data-action="open-task" data-task="${attr(task.parent_id)}">${esc(task.parent_id)}</button>` : '—（根任务）'}</dd>
          <dt>目标版本</dt><dd>r${esc(task.revision)}</dd>
          <dt>工作区</dt><dd class="mono tiny">${esc(task.workspace)}</dd>
          <dt>创建时间</dt><dd>${esc(stamp(task.created))}</dd>
          <dt>依赖</dt><dd>${(task.dependencies || []).map((id) => `<button class="btn sm" data-action="open-task" data-task="${attr(id)}">${esc(id)}</button>`).join(' ') || '—'}</dd>
          <dt>等待</dt><dd>${task.wait ? esc(task.wait.note || '等待匹配事件') + `<br><span class="tiny muted">任务 ${esc((task.wait.task_ids || []).join('、') || '—')} · 主题 ${esc((task.wait.topics || []).join('、') || '—')} · ${esc(task.wait.mode || 'any')}${task.wait.deadline ? ' · 截止 ' + esc(clock(task.wait.deadline)) : ''}</span>` : '—'}</dd>
        </dl>
      </div>

      <div class="row wrap">
        ${['failed', 'waiting', 'completed'].includes(task.status) && !task.superseded ? '<button class="btn sm" data-action="resume-task">继续任务</button>' : ''}
        <button class="btn sm" data-action="compact-task">下轮 compact</button>
        <span class="spacer"></span>
        ${!isTerminal(task.status) ? '<button class="btn sm danger" data-action="cancel-task">取消此任务</button>' : ''}
      </div>

      <form class="drawer-section" data-action="send-message">
        <h3>给这个 Agent 补充信息</h3>
        <div class="field">
          <textarea name="summary" rows="3" required placeholder="新信息、约束或需要它处理的问题"></textarea>
        </div>
        <div class="row" style="margin-top:8px"><span class="spacer"></span><button class="btn sm primary" type="submit">发送并唤醒</button></div>
      </form>

      ${task.result ? `<div class="drawer-section"><h3>任务结果</h3><div class="card"><div class="card-body entry-body">${rich(task.result)}</div></div></div>` : ''}

      ${reports?.length ? reports.map((report) => `<div class="drawer-section">${renderReport(report)}</div>`).join('') : ''}

      ${changes?.length ? `<div class="drawer-section"><h3>文件变更（${changes.length}）</h3>
        ${changes.map((change) => `<details class="file-row"><summary><span class="file-path">${esc(change.path)}</span><span class="file-meta">${change.before ? '修改' : '新增'}</span></summary><div class="file-body">${change.diff ? diffBlock(change.diff) : '<div class="empty"><p>二进制差异</p></div>'}</div></details>`).join('')}
      </div>` : ''}

      ${task.omitted_files?.length ? `<div class="drawer-section"><h3>未纳入扫描（${task.omitted_files.length}）</h3><pre class="code">${esc(task.omitted_files.join('\n'))}</pre></div>` : ''}

      <div class="drawer-section">
        <h3>收件箱（${(messages || []).length} 条，${pending.length} 条未读）</h3>
        ${messages?.length ? messages.slice().reverse().slice(0, 40).map((message) => `
          <div class="msg-row">
            <div class="msg-head"><span>${esc(message.kind || 'message')}</span><span>·</span><span>${esc(message.delivery)}</span><span>·</span><span>${esc(message.consumed ? '已装入上下文' : '待读')}</span><span>·</span><span>${esc(clock(message.created))}</span></div>
            <div>${esc(truncate(message.summary, 400))}</div>
          </div>`).join('') : '<div class="note">没有消息</div>'}
      </div>

      <details class="raw"><summary>有效上下文（epoch ${esc(task.context_epoch)}，${(task.history || []).length} 条消息）</summary>
        <pre class="code">${esc(JSON.stringify(task.history || [], null, 2).slice(0, 24000))}</pre>
      </details>

      <details class="raw"><summary>任务事件（${(events || []).length}）</summary>
        <pre class="code">${esc((events || []).slice(-40).map((event) => `${clock(event.created)}  ${event.type}  ${event.task_id || ''}`).join('\n'))}</pre>
      </details>
    </div>`;
}

function benchmarkOf(model) {
  const source = model.benchmark || model.benchmarks || model.scores || model.score || {};
  const number = (...values) => {
    const value = values.find((item) => item !== undefined && item !== null && item !== '');
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  return {
    overall: number(source.overall, source.overall_score, model.benchmark_overall, model.overall),
    coding: number(source.coding, source.coding_score, model.benchmark_coding),
    reasoning: number(source.reasoning, source.reasoning_score, model.benchmark_reasoning),
    agent: number(source.agent, source.agentic, source.agent_score, model.benchmark_agent),
  };
}

export function modelOverall(model) {
  return benchmarkOf(model).overall;
}

export function sortModelsByBenchmark(models) {
  return [...models].sort((a, b) => {
    const left = modelOverall(a);
    const right = modelOverall(b);
    if (left === null && right === null) return String(a.label || a.id).localeCompare(String(b.label || b.id));
    if (left === null) return 1;
    if (right === null) return -1;
    return right - left;
  });
}

function scoreText(value) {
  return value === null ? '—' : Number(value).toFixed(1).replace(/\.0$/, '');
}

/** 健康状态的三态文案：未验证 / 可用（含证据来源与时间）/ 需处理（含该做什么）。 */
function healthOf(model) {
  return model && model.health ? model.health : null;
}

function healthAge(seconds) {
  if (!seconds) return '未知时间';
  const delta = Math.max(0, Date.now() / 1000 - seconds);
  if (delta < 3600) return `${Math.round(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.round(delta / 3600)} 小时前`;
  return `${Math.round(delta / 86400)} 天前`;
}

/** 额度与档位后缀：让人一眼看到"这个模型同桶最多几个在途、属于哪一档"。 */
function capacityText(model) {
  const capacity = model && model.capacity;
  if (!capacity) return '';
  const tier = model.tier && model.tier !== 'standard' ? `${model.tier} 档 · ` : '';
  const tpm = capacity.tpm ? ` · tpm ${capacity.tpm}` : '';
  return ` · ${tier}并发 ${capacity.concurrency}${tpm}`;
}

function availabilityText(model) {
  const health = healthOf(model);
  if (!health) return '';
  if (health.state === 'ok') {
    const source = health.source === 'call' ? '真实调用' : '探测';
    return health.stale ? ` · 待复验（上次${source} ${healthAge(health.checked_at)}）`
                        : ` · 可用（${source}，${healthAge(health.checked_at)}）`;
  }
  if (health.state === 'blocked') return ` · 需处理（${health.code || 'FAIL'}）`;
  return ' · 未验证';
}

export function modelAvailabilityWarning(model) {
  if (!model) return '';
  const health = healthOf(model);
  if (!health || health.state === 'ok') return '';
  if (health.state === 'blocked') {
    return `该模型上次失败：${health.code || 'FAIL'}。${health.reason || ''}` +
      (health.remedy ? `\n处理方式：${health.remedy}` : '') +
      '\n重试无用——这是配置或权限问题，改完配置后标记会自动清除。';
  }
  return `该模型尚未验证或证据已过期${health.stale ? '（上次证据在 ' + healthAge(health.checked_at) + '）' : ''}。` +
    '直接创建任务会在第一次调用时才知道结果；也可以先点“复验过期的模型”。';
}

export function modelOptionLabel(model) {
  const scores = benchmarkOf(model);
  const name = model.label || model.id;
  const state = availabilityText(model) + capacityText(model);
  if (scores.overall === null) return `${name} · 未匹配评分${state}`;
  return `${name} · 综合 ${scoreText(scores.overall)} · 编程 ${scoreText(scores.coding)} · 推理 ${scoreText(scores.reasoning)} · Agent ${scoreText(scores.agent)}${state}`;
}

export function renderBlob({ ref, content, total, label }) {
  const loaded = content.length;
  return `
    <div class="drawer-head">
      <h2>${esc(label || '内容对象')}</h2>
      <button class="icon-btn" data-action="close-drawer" aria-label="关闭">✕</button>
    </div>
    <div class="drawer-body">
      <div class="row wrap tiny muted">
        <span class="mono">${esc(ref.slice(0, 20))}…</span>
        <span class="spacer"></span>
        <span>已加载 ${esc(loaded)} / ${esc(total)} 字符</span>
      </div>
      <pre class="code">${esc(content)}</pre>
      ${loaded < total ? `<div class="row"><span class="tiny muted">内容已截断</span><span class="spacer"></span><button class="btn sm" data-action="blob-more" data-ref="${attr(ref)}" data-offset="${loaded}" data-label="${attr(label || '')}">加载更多</button></div>` : ''}
    </div>`;
}

/* ------------------------------------------------- 模型 / Provider 目录 */

const API_LABELS = {
  'openai-completions': 'Chat Completions',
  'openai-responses': 'Responses（特殊接口）',
};

function apiPill(api) {
  if (!api) return quietPill('未知接口');
  return `<span class="pill ${api === 'openai-responses' ? 'st-waiting' : 'st-active'}"><i class="dot"></i>${esc(API_LABELS[api] || api)}</span>`;
}

function availabilityPill(entry) {
  const health = healthOf(entry.model || entry) || (entry.reachability ? {
    state: entry.reachability.reachable ? 'ok' : 'blocked',
    code: entry.reachability.code, reason: entry.reachability.error,
    source: entry.reachability.source || 'probe', checked_at: entry.reachability.checked_at, stale: true,
  } : null);
  if (!health) return entry.advertised === false ? '<span class="benchmark-unmatched">未广告</span>' : '';
  const tip = `${health.reason || ''}${health.remedy ? '\n处理：' + health.remedy : ''}`;
  if (health.state === 'ok') {
    const source = health.source === 'call' ? '真实调用' : '探测';
    return health.stale
      ? `<span class="pill st-waiting" title="证据来自${source}，${healthAge(health.checked_at)}，已过期"><i class="dot"></i>待复验</span>`
      : `<span class="pill st-completed" title="证据来自${source}，${healthAge(health.checked_at)}"><i class="dot"></i>可用</span>`;
  }
  if (health.state === 'blocked')
    return `<span class="pill st-failed" title="${attr(tip)}"><i class="dot"></i>${esc(health.code || '需处理')}</span>`;
  return '<span class="pill" title="还没有可用性证据"><i class="dot"></i>未验证</span>';
}

function capacityLine(entry) {
  const model = entry.model || {};
  const capacity = model.capacity;
  if (!capacity) return '';
  const tier = model.tier && model.tier !== 'standard' ? ` · ${model.tier} 档` : '';
  const tpm = capacity.tpm ? ` · tpm ${capacity.tpm}` : '';
  const rpm = capacity.rpm ? ` · rpm ${capacity.rpm}` : '';
  return `并发上限 ${capacity.concurrency}${tpm}${rpm}${tier}`;
}

function providerModelRow(entry) {
  const model = entry.model || {};
  const scores = benchmarkOf(model);
  const scoreHtml = scores.overall === null
    ? '<span class="benchmark-unmatched">未匹配评分</span>'
    : `<span class="benchmark-score is-overall">综合 <b>${esc(scoreText(scores.overall))}</b></span>
       <span class="benchmark-score">Coding <b>${esc(scoreText(scores.coding))}</b></span>
       <span class="benchmark-score">Reasoning <b>${esc(scoreText(scores.reasoning))}</b></span>
       <span class="benchmark-score">Agent <b>${esc(scoreText(scores.agent))}</b></span>`;
  return `<div class="model-row">
    <div class="model-main">
      <div class="model-name">${esc(entry.name || entry.id)}
        ${entry.default ? '<span class="pill st-active"><i class="dot"></i>默认</span>' : ''}
        ${availabilityPill(entry)}
        ${entry.role && entry.role !== 'available' ? quietPill(entry.role === 'review' ? '评审' : entry.role) : ''}
      </div>
      <div class="model-meta">${esc(entry.id)} · 上下文 ${esc(entry.contextWindow)} · 输出 ${esc(entry.maxTokens)}${model.configured ? '' : ' · 未配置凭据'}</div>
    ${capacityLine(entry) ? `<div class="tiny muted">${esc(capacityLine(entry))}</div>` : ''}
      ${entry.description ? `<div class="tiny muted">${esc(entry.description)}</div>` : ''}
      <div class="benchmark-line">${scoreHtml}</div>
    </div>
    <span class="spacer"></span>
    ${entry.default ? '' : `<button class="btn sm" data-action="set-default-model" data-provider="${attr(entry.providerId)}" data-model="${attr(entry.id)}">设为默认</button>`}
  </div>`;
}

function providerCard(provider) {
  return `<div class="provider-setup">
    <div class="provider-setup-head">
      <div>
        <h3>${esc(provider.name)} <span class="mono tiny">${esc(provider.id)}</span></h3>
        <p>${esc(provider.base_url)} · ${provider.configured ? '凭据已配置' : '未配置凭据'}${provider.key_env ? ' · 环境变量 ' + esc(provider.key_env) : ''}</p>
      </div>
      ${apiPill(provider.api)}
      <button class="btn sm" data-action="scan-provider" data-provider="${attr(provider.id)}">扫描模型</button>
      <button class="btn sm" data-action="verify-provider" data-provider="${attr(provider.id)}" title="只复验已过期或未验证的模型；每次最多 3 个，每个消耗一次真实调用，可以再点">复验过期的模型（≤3）</button>
    </div>
    <div class="model-list">${provider.models.map((entry) => providerModelRow({ ...entry, providerId: provider.id })).join('') || '<div class="note">该 provider 还没有模型，点击“扫描模型”从 /models 拉取</div>'}</div>
  </div>`;
}

export function renderProviders({ providers, unmanaged, appConfig, importError, refreshError }) {
  const provider = appConfig?.provider || null;
  const warnings = [];
  if (importError) warnings.push(`内置配置加载失败：${importError}`);
  if (appConfig?.catalog_error) warnings.push(`模型目录：${appConfig.catalog_error}`);
  if (appConfig?.discovery_error) warnings.push(`模型扫描：${appConfig.discovery_error}`);
  if (refreshError) warnings.push(refreshError);
  return `
    ${warnings.length ? `<div class="banner warn"><div class="banner-main">
      <h4>配置提示</h4>
      <div class="banner-body">${warnings.map((item) => esc(item)).join('<br>')}</div>
    </div></div>` : ''}
    ${provider ? `<div class="config-summary">
      <div class="config-summary-head">
        <div>
          <h3>当前默认模型：${esc(provider.name)} · ${esc(appConfig.model || '—')}</h3>
          <p>来源格式 ${esc(appConfig.source_format || '—')} · 默认模型本地 ID <span class="mono">${esc(appConfig.model_id || '—')}</span></p>
        </div>
        ${apiPill(providers.find((item) => item.id === provider.id)?.api)}
      </div>
      <div class="config-summary-grid">
        <div><span class="config-summary-label">Provider</span><div class="mono">${esc(provider.id)}</div></div>
        <div><span class="config-summary-label">接口地址</span><div class="mono">${esc(provider.base_url || '—')}</div></div>
        <div><span class="config-summary-label">wire api</span><div class="mono">${esc(provider.wire_api || '—')}</div></div>
        <div><span class="config-summary-label">模型数</span><div class="mono">${esc((provider.models || []).length)}</div></div>
      </div>
    </div>` : ''}
    <div class="model-list-head">
      <div>
        <strong>Provider 目录（${providers.length}）</strong>
        <span class="hint">同一 provider 下的模型共享接口地址、协议与请求头；默认模型不会被自动改写。</span>
      </div>
    </div>
    <div class="model-list">${providers.map(providerCard).join('') || '<div class="note">还没有 provider。粘贴 DSH YAML 或 Codex TOML 后导入即可。</div>'}</div>
    ${unmanaged?.length ? `<div class="model-list-head"><div><strong>未归入 Provider 的模型（${unmanaged.length}）</strong>
      <span class="hint">例如离线演示记录；它们不使用统一 provider 结构。</span></div></div>
      <div class="model-list">${unmanaged.map((model) => `<div class="model-row"><div class="model-main">
        <div class="model-name">${esc(model.label || model.id)}</div>
        <div class="model-meta">${esc(model.model || model.id)} · ${esc(model.base_url || '内置')}</div>
        <div class="tiny muted">接口 ${esc(API_LABELS[model.api] || model.api || '—')} · 上下文 ${esc(model.context_window || '—')} · 输出 ${esc(model.max_output || '—')}</div>
      </div><span class="spacer"></span></div>`).join('')}</div>` : ''}`;
}

/* ------------------------------------------------------- 技能（skill）目录 */

const RANK_HINTS = {
  'workspace-dsh': '任务工作区 .dsh/skills',
  'workspace-agents': '任务工作区 .agents/skills',
  'project-root': '项目根及其祖先目录',
  custom: '自定义根',
  'user-dsh': '用户 ~/.dsh/skills',
  'user-agents': '用户 ~/.agents/skills',
  bundled: '随程序分发',
};

/** 技能目录：先给"谁能胜出"的规则，再按 rank 分组列出技能本体。 */
export function renderSkills({ skills, diagnostics, roots, workspace, bundled_dir }, currentWorkspace) {
  const list = skills || [];
  const groups = new Map();
  for (const skill of list) {
    const key = `${skill.rank}:${skill.source}`;
    if (!groups.has(key)) groups.set(key, { source: skill.source, rank: skill.rank, items: [] });
    groups.get(key).items.push(skill);
  }
  const ordered = [...groups.values()].sort((a, b) => a.rank - b.rank);
  const problems = diagnostics || [];
  return `
    <div class="note">扫描位置：<span class="mono">${esc(currentWorkspace || workspace || '—')}</span>
      ${list.length ? `· 共 ${esc(list.length)} 个技能` : '· 没有发现技能'}</div>
    ${problems.length ? `<div class="banner warn"><div class="banner-main">
      <h4>${problems.length} 条技能诊断</h4>
      <div class="banner-body">${problems.map((item) => `${esc(item.error)}<br><span class="mono tiny">${esc(item.path || item.root || '')}</span>`).join('<br>')}</div>
    </div></div>` : ''}
    ${list.length ? `<div class="model-list">${ordered.map((group) => `
      <div class="model-list-head"><div>
        <strong>${esc(RANK_HINTS[group.source] || group.source)}（${group.items.length}）</strong>
        <span class="hint">rank ${esc(group.rank)} · 越小越优先</span>
      </div></div>
      ${group.items.map((skill) => `
        <div class="model-row">
          <div class="model-main">
            <div class="model-name">${esc(skill.name)}</div>
            <div class="tiny muted">${esc(skill.description)}</div>
            ${skill.when_to_use ? `<div class="tiny muted">适用：${esc(skill.when_to_use)}</div>` : ''}
            <div class="model-meta">${esc(skill.path)}</div>
            ${skill.invocable ? `<div class="tiny muted">端口可调：<span class="mono">POST /api/skills/call</span>${skill.batch ? ` · 批量上限 ${esc(skill.max_batch)} 份` : ' · 仅单次调用'}</div>` : ''}
          </div>
          <span class="spacer"></span>
          ${skill.invocable ? '<span class="pill">端口可调</span>' : ''}
          ${skill.model_invocable ? '' : '<span class="pill">模型不可用</span>'}
          <button class="btn sm" data-action="open-skill" data-skill="${attr(skill.name)}">查看</button>
        </div>`).join('')}
    `).join('')}</div>` : '<div class="note">把技能写成目录包 <code>&lt;根&gt;/skills/&lt;name&gt;/SKILL.md</code>，或扁平文件 <code>&lt;根&gt;/&lt;name&gt;.md</code>，重新扫描即可出现在这里。</div>'}
    ${roots?.length ? `<div class="note">已扫描根目录（按优先级）：${roots.map((root) => `<span class="mono">${esc(root)}</span>`).join(' · ')}</div>` : ''}
    ${bundled_dir ? `<div class="note">内置技能包：<span class="mono">${esc(bundled_dir)}</span></div>` : ''}`;
}

/** 技能详情：正文与"模型实际加载到的形状"分开显示，避免把渲染包装当成技能本身。 */
export function renderSkillDetail({ name, skill, content, error, loading }) {
  if (loading) {
    return `<div class="drawer-head"><h2>${esc(name)}</h2>
      <button class="icon-btn" data-action="close-drawer" aria-label="关闭">✕</button></div>
      <div class="drawer-body"><div class="note">正在加载技能…</div></div>`;
  }
  if (error) {
    return `<div class="drawer-head"><h2>${esc(name)}</h2>
      <button class="icon-btn" data-action="close-drawer" aria-label="关闭">✕</button></div>
      <div class="drawer-body"><div class="banner error"><div class="banner-main"><h4>无法加载</h4>
      <div class="banner-body">${esc(error)}</div></div></div></div>`;
  }
  const meta = skill || {};
  return `
    <div class="drawer-head">
      <h2>${esc(meta.name || name)}</h2>
      <button class="icon-btn" data-action="close-drawer" aria-label="关闭">✕</button>
    </div>
    <div class="drawer-body">
      <div class="row wrap">
        ${monoPill(meta.source || '')}
        <span class="tiny muted">rank ${esc(meta.rank ?? '—')}</span>
        <span class="spacer"></span>
        <span class="tiny muted">${meta.model_invocable ? '模型可加载' : '模型不可加载'}</span>
      </div>
      <div class="drawer-section">
        <h3>摘要</h3>
        <div class="tiny">${esc(meta.description || '—')}</div>
        ${meta.when_to_use ? `<div class="tiny muted">适用：${esc(meta.when_to_use)}</div>` : ''}
        <div class="tiny muted">base directory：<span class="mono">${esc(meta.base || '—')}</span></div>
        <div class="tiny muted">文件：<span class="mono">${esc(meta.path || '—')}</span></div>
      </div>
      <div class="drawer-section">
        <h3>调用契约</h3>
        ${meta.invocable ? `<div class="tiny">可通过端口调用：<span class="mono">${esc(meta.invoke_endpoint)}</span></div>
          <div class="tiny muted">批量：${meta.batch ? `支持，上限 ${esc(meta.max_batch)} 份` : '不支持（仅单次 args）'} · 超时 ${esc(meta.timeout_seconds)} 秒</div>
          <div class="tiny muted">轮询：<span class="mono">${esc(meta.jobs_endpoint)}</span></div>` :
          '<div class="tiny muted">纯指令技能：只能用 <code>skill</code> 加载正文，不能被端口调用。</div>'}
      </div>
      <div class="drawer-section">
        <h3>模型加载到的内容</h3>
        <div class="tiny muted">模型调用 <code>skill</code> 工具时看到的就是这一段（含资源基目录提示）。</div>
        <pre class="code">${esc(content || '')}</pre>
      </div>
    </div>`;
}
