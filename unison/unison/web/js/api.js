/**
 * Unison Console —— HTTP 与事件流客户端。
 * 只依赖浏览器原生 fetch / EventSource，没有第三方运行时依赖。
 */

export class ApiError extends Error {
  constructor(message, { status = 0, path = '', cause = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
    this.cause = cause;
  }
}

const DEFAULT_TIMEOUT = 60000;

export function createClient({ timeout = DEFAULT_TIMEOUT } = {}) {
  async function request(path, { method = 'GET', body, query, signal, timeoutMs = timeout } = {}) {
    const url = new URL('/api/' + String(path).replace(/^\/+/, ''), location.origin);
    if (query) {
      for (const [key, value] of Object.entries(query)) {
        if (value === undefined || value === null || value === '') continue;
        url.searchParams.set(key, String(value));
      }
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new DOMException('timeout', 'TimeoutError')), timeoutMs);
    const relay = () => controller.abort(signal?.reason);
    if (signal) signal.addEventListener('abort', relay, { once: true });

    let response;
    try {
      response = await fetch(url, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (error) {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', relay);
      if (signal?.aborted) throw new ApiError('请求已取消', { path: url.pathname, cause: error });
      if (controller.signal.aborted) throw new ApiError('请求超时，服务可能仍在后台执行', { path: url.pathname, cause: error });
      throw new ApiError('无法连接本地服务', { path: url.pathname, cause: error });
    }
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', relay);

    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (error) {
        throw new ApiError(`服务返回了非 JSON 响应（HTTP ${response.status}）`, { status: response.status, path: url.pathname, cause: error });
      }
    }
    if (!response.ok) {
      const message = (data && (data.error || data.message)) || response.statusText || `HTTP ${response.status}`;
      throw new ApiError(message, { status: response.status, path: url.pathname });
    }
    return data;
  }

  return {
    request,
    get: (path, query, signal) => request(path, { query, signal }),
    // POST 默认 120 秒；归档这类真要干活的动作由调用方给更长的窗口（服务端窗口 300 秒）。
    post: (path, body, options) => request(path, { method: 'POST', body: body ?? {}, timeoutMs: 120000, ...options }),
  };
}

/**
 * 事件流：服务端每次只推送最新游标，客户端据此重新拉取状态。
 * 断线后使用最新游标重连，避免错过事件。
 */
export function openStream({ onTick, onState }) {
  let cursor = 0;
  let source = null;
  let retry = 0;
  let closed = false;
  let reconnectTimer = null;

  function connect() {
    if (closed) return;
    source = new EventSource(`/api/stream?after=${cursor}`);
    source.onopen = () => {
      retry = 0;
      onState?.('online');
    };
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (typeof payload.cursor === 'number' && payload.cursor > cursor) cursor = payload.cursor;
      } catch {
        /* 心跳或未知负载：仍然触发一次刷新 */
      }
      onTick?.(cursor);
    };
    source.onerror = () => {
      onState?.('connecting');
      source?.close();
      source = null;
      if (closed) return;
      const delay = Math.min(8000, 500 * 2 ** retry++);
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, delay);
    };
  }

  function seed(value) {
    if (typeof value === 'number' && value > cursor) cursor = value;
  }

  function close() {
    closed = true;
    clearTimeout(reconnectTimer);
    source?.close();
    source = null;
  }

  connect();
  return { seed, close, get cursor() { return cursor; } };
}
