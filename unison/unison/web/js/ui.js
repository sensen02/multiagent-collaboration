/** 通用格式化、模板与轻量组件。所有插值都必须经过 esc()。 */

export const STATUS = {
  active: { label: '运行中', tone: 'active' },
  running: { label: '执行中', tone: 'running' },
  queued: { label: '排队中', tone: 'queued' },
  waiting: { label: '等待事件', tone: 'waiting' },
  completed: { label: '已完成', tone: 'completed' },
  failed: { label: '需要处理', tone: 'failed' },
  paused: { label: '已暂停', tone: 'paused' },
  cancelled: { label: '已停止', tone: 'cancelled' },
  superseded: { label: '已弃置', tone: 'superseded' },
  open: { label: '待回答', tone: 'waiting' },
  answered: { label: '已回答', tone: 'completed' },
  obsolete: { label: '已过时', tone: 'superseded' },
};

export const VERDICT = {
  failed: { label: '验证失败', tone: 'warn' },
  verified: { label: '已声明验证', tone: 'success' },
  not_verified: { label: '未验证', tone: 'muted' },
  unknown: { label: '验证状态未知', tone: 'warn' },
};

/* 证据只显示**事实**，不显示运行时给的评级。
   运行时不再对证据分级（`verified_by_command` / `_weak` / `_inspection` 已移除）：
   命令原文、退出码、是否超时都摆在报告里，够不够由人判断。 */

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function attr(value) {
  return esc(value).replace(/\n/g, ' ');
}

export function statusOf(status) {
  return STATUS[status] || { label: status || '未知', tone: 'queued' };
}

export function pill(status, extra = '') {
  const meta = statusOf(status);
  return `<span class="pill st-${esc(meta.tone)}${extra ? ' ' + extra : ''}"><i class="dot"></i>${esc(meta.label)}</span>`;
}

export function quietPill(text, extra = '') {
  return `<span class="pill quiet${extra ? ' ' + extra : ''}">${esc(text)}</span>`;
}

export function monoPill(text) {
  return `<span class="pill mono">${esc(text)}</span>`;
}

/* --------------------------------------------------------------- 时间格式 */

const timeFmt = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const dayFmt = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' });

export function clock(seconds) {
  if (!seconds) return '—';
  return timeFmt.format(new Date(seconds * 1000));
}

export function stamp(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  const sameDay = new Date().toDateString() === date.toDateString();
  return sameDay ? timeFmt.format(date) : `${dayFmt.format(date)} ${timeFmt.format(date)}`;
}

export function relative(seconds) {
  if (!seconds) return '—';
  const delta = Date.now() / 1000 - seconds;
  if (delta < 5) return '刚刚';
  if (delta < 60) return `${Math.floor(delta)} 秒前`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  return `${Math.floor(delta / 86400)} 天前`;
}

export function duration(from, to) {
  if (!from) return '—';
  const end = to || Date.now() / 1000;
  const delta = Math.max(0, end - from);
  if (delta < 60) return `${delta.toFixed(delta < 10 ? 1 : 0)} 秒`;
  if (delta < 3600) return `${Math.floor(delta / 60)} 分 ${Math.round(delta % 60)} 秒`;
  return `${Math.floor(delta / 3600)} 小时 ${Math.round((delta % 3600) / 60)} 分`;
}

export function bytes(size) {
  if (!Number.isFinite(size)) return '—';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

export function truncate(text, max = 160) {
  const value = String(text ?? '');
  return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

/* ------------------------------------------------------------- 富文本渲染 */

function inline(text) {
  return esc(text)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/^###\s+(.+)$/gm, '<strong>$1</strong>')
    .replace(/^##\s+(.+)$/gm, '<strong>$1</strong>')
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>');
}

/** 极简安全渲染：先转义，再识别围栏代码块、行内代码和粗体。 */
export function rich(text) {
  const source = String(text ?? '');
  if (!source.trim()) return '';
  const parts = source.split('```');
  return parts
    .map((chunk, index) => {
      if (index % 2 === 1) {
        const newline = chunk.indexOf('\n');
        const language = newline >= 0 ? chunk.slice(0, newline).trim() : '';
        const code = newline >= 0 ? chunk.slice(newline + 1) : chunk;
        return `<pre class="code"${language ? ` data-lang="${attr(language)}"` : ''}>${esc(code.replace(/\n$/, ''))}</pre>`;
      }
      const body = chunk.trim();
      return body ? `<p>${inline(body)}</p>` : '';
    })
    .join('');
}

export function jsonBlock(value, { open = false, label = '原始数据', scroll = true } = {}) {
  if (value === undefined || value === null) return '';
  let text;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  if (text === undefined || text === '{}' || text === 'null') return '';
  return `<details class="raw"${open ? ' open' : ''}><summary>${esc(label)}</summary><pre class="code${scroll ? ' scroll' : ''}">${esc(truncateLong(text))}</pre></details>`;
}

function truncateLong(text, max = 24000) {
  return text.length > max ? `${text.slice(0, max)}\n… 已截断 ${text.length - max} 字符` : text;
}

export function diffBlock(diff) {
  const text = String(diff ?? '');
  if (!text.trim()) return '<div class="empty"><p>没有文本差异</p></div>';
  const lines = text.split('\n').map((line) => {
    let cls = '';
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) cls = 'meta';
    else if (line.startsWith('@@')) cls = 'hunk';
    else if (line.startsWith('+')) cls = 'add';
    else if (line.startsWith('-')) cls = 'del';
    return `<span class="ln ${cls}">${esc(line)}</span>`;
  });
  return `<pre class="diff">${lines.join('')}</pre>`;
}

/* ------------------------------------------------------------------ toast */

const TONES = { info: 'info', success: 'success', warn: 'warn', error: 'error' };

export function toast(message, tone = 'info', ttl = 4200) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const node = document.createElement('div');
  node.className = 'toast';
  node.dataset.tone = TONES[tone] || 'info';
  node.innerHTML = `<i class="toast-dot"></i><span>${esc(message)}</span>`;
  host.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity 160ms ease';
    setTimeout(() => node.remove(), 200);
  }, ttl);
}

/* ------------------------------------------------------------- 小工具函数 */

export function countBy(items, key) {
  const map = new Map();
  for (const item of items) map.set(item[key], (map.get(item[key]) || 0) + 1);
  return map;
}

export function pluralize(count, singular, plural) {
  return `${count} ${count === 1 ? singular : (plural || singular)}`;
}

export function taskTreeOf(tasks) {
  const byParent = new Map();
  for (const task of tasks) {
    const key = task.parent_id || '';
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(task);
  }
  for (const list of byParent.values()) list.sort((a, b) => a.created - b.created);
  return byParent;
}

export function descendants(tasks, rootId) {
  const byParent = taskTreeOf(tasks);
  const out = [];
  const walk = (id, depth) => {
    for (const child of byParent.get(id) || []) {
      out.push({ task: child, depth });
      walk(child.id, depth + 1);
    }
  };
  walk(rootId, 0);
  return out;
}

export function isTerminal(status) {
  return ['completed', 'failed', 'cancelled', 'superseded'].includes(status);
}
