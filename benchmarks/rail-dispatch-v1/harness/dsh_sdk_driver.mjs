#!/usr/bin/env node
/** Drive four sequential benchmark stages over dsh SDK newline JSON-RPC. */
import { spawn } from 'node:child_process';
import { createWriteStream, promises as fs } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import readline from 'node:readline';

function usage(code = 0) {
  const out = code ? process.stderr : process.stdout;
  out.write(`Usage: dsh_sdk_driver.mjs [options] stage0 stage1 stage2 stage3\n\n` +
    `Options:\n  --cwd DIR             session working directory (required)\n` +
    `  --output DIR          artifact directory (default: dsh-sdk-output)\n` +
    `  --session ID          session id (default: generated UUID)\n` +
    `  --max-tokens N        per-turn output cap (default: 32768)\n` +
    `  --stage-timeout SEC   timeout per stage (default: 1800)\n` +
    `  --rpc-timeout SEC     timeout per RPC response (default: 60)\n` +
    `  --dsh PATH            dsh executable (default: dsh)\n` +
    `  --profile NAME        dsh profile name (default: sdk)\n  -h, --help            show help\n`);
  process.exit(code);
}

function parseArgs(argv) {
  const opts = { output: 'dsh-sdk-output', maxTokens: 32768, stageTimeout: 1800, rpcTimeout: 60, dsh: 'dsh', profile: 'sdk' };
  const files = [];
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '-h' || arg === '--help') usage();
    if (['--cwd', '--output', '--session', '--max-tokens', '--stage-timeout', '--rpc-timeout', '--dsh', '--profile'].includes(arg)) {
      if (++i >= argv.length) usage(2);
      const key = { '--cwd': 'cwd', '--output': 'output', '--session': 'session', '--max-tokens': 'maxTokens', '--stage-timeout': 'stageTimeout', '--rpc-timeout': 'rpcTimeout', '--dsh': 'dsh', '--profile': 'profile' }[arg];
      opts[key] = ['maxTokens', 'stageTimeout', 'rpcTimeout'].includes(key) ? Number(argv[i]) : argv[i];
    } else if (arg.startsWith('-')) usage(2);
    else files.push(arg);
  }
  if (!opts.cwd || files.length !== 4 || !Number.isSafeInteger(opts.maxTokens) || opts.maxTokens <= 0 || opts.stageTimeout <= 0 || opts.rpcTimeout <= 0) usage(2);
  opts.files = files;
  opts.session ??= `rail-dispatch-${randomUUID()}`;
  return opts;
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }
function timestamp() { return new Date().toISOString(); }

const opts = parseArgs(process.argv.slice(2));
const output = resolve(opts.output);
await fs.mkdir(output, { recursive: true });
const notificationLog = createWriteStream(resolve(output, 'notifications.jsonl'), { flags: 'w', encoding: 'utf8' });
const stderrLog = createWriteStream(resolve(output, 'dsh-stderr.log'), { flags: 'w', encoding: 'utf8' });
const child = spawn(opts.dsh, ['--profile', opts.profile], { stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env } });
child.stderr.pipe(stderrLog);
let nextId = 1;
const pending = new Map();
const waiters = [];
let childFailure;
let lastAssistant = null;
let notificationCount = 0;
const routeEvidence = new Map();

function collectRoutes(value) {
  if (!value || typeof value !== 'object') return;
  if (!Array.isArray(value)) {
    const provider = typeof value.provider === 'string' ? value.provider : undefined;
    const model = typeof value.model === 'string' ? value.model : undefined;
    if (provider || model) {
      const route = { ...(provider ? { provider } : {}), ...(model ? { model } : {}) };
      routeEvidence.set(JSON.stringify(route), route);
    }
  }
  for (const child of Object.values(value)) collectRoutes(child);
}

function rejectAll(error) {
  childFailure = error;
  for (const { reject, timer } of pending.values()) { clearTimeout(timer); reject(error); }
  pending.clear();
  while (waiters.length) { const waiter = waiters.shift(); clearTimeout(waiter.timer); waiter.reject(error); }
}
child.on('error', rejectAll);
child.on('exit', (code, signal) => {
  if (code !== 0 && !childFailure) rejectAll(new Error(`dsh exited code=${code} signal=${signal}`));
});

function rpc(method, params, timeoutSec = opts.rpcTimeout) {
  if (childFailure) return Promise.reject(childFailure);
  const id = nextId++;
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`RPC ${method} timed out`)); }, timeoutSec * 1000);
    pending.set(id, { resolve: resolvePromise, reject, timer });
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, ...(params === undefined ? {} : { params }) }) + '\n');
  });
}

function notify(frame) {
  notificationCount++;
  notificationLog.write(JSON.stringify({ receivedAt: timestamp(), ...frame }) + '\n');
  collectRoutes(frame.params);
  if (frame.method === 'session.event' && frame.params?.sessionId === opts.session) {
    const event = frame.params.event;
    const serialized = JSON.stringify(event);
    if (/assistant/i.test(serialized)) lastAssistant = event;
  }
  for (let i = waiters.length - 1; i >= 0; i--) {
    if (waiters[i].predicate(frame)) {
      const waiter = waiters.splice(i, 1)[0]; clearTimeout(waiter.timer); waiter.resolve(frame);
    }
  }
}

const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
lines.on('line', line => {
  let frame;
  try { frame = JSON.parse(line); } catch { rejectAll(new Error('invalid JSON-RPC line from dsh')); return; }
  if (frame.id !== undefined && !frame.method) {
    const item = pending.get(frame.id); if (!item) return;
    pending.delete(frame.id); clearTimeout(item.timer);
    if (frame.error) item.reject(new Error(`RPC error ${frame.error.code}: ${frame.error.message}`));
    else item.resolve(frame.result);
  } else if (frame.method && frame.id === undefined) notify(frame);
});

function waitFor(predicate, timeoutSec, label) {
  return new Promise((resolvePromise, reject) => {
    const timer = setTimeout(() => {
      const pos = waiters.findIndex(x => x.resolve === resolvePromise);
      if (pos >= 0) waiters.splice(pos, 1);
      reject(new Error(`${label} timed out`));
    }, timeoutSec * 1000);
    waiters.push({ predicate, resolve: resolvePromise, reject, timer });
  });
}

async function terminate() {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([new Promise(r => child.once('exit', r)), delay(3000)]);
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
}

const summary = { sessionId: opts.session, provider: 'audit', model: 'gpt-5.6-sol', cwd: resolve(opts.cwd), startedAt: timestamp(), stages: [] };
let ok = false;
try {
  summary.server = await rpc('initialize', { provider: 'audit', model: 'gpt-5.6-sol', cwd: resolve(opts.cwd), maxTokens: opts.maxTokens });
  for (let index = 0; index < opts.files.length; index++) {
    const text = await fs.readFile(opts.files[index], 'utf8');
    const stage = { stage: index, promptFile: resolve(opts.files[index]), startedAt: timestamp() };
    const begun = Date.now();
    const running = waitFor(f => f.method === 'session.status' && f.params?.sessionId === opts.session && f.params?.status === 'running', opts.stageTimeout, `stage${index} running`);
    const idle = waitFor(f => f.method === 'session.status' && f.params?.sessionId === opts.session && f.params?.status === 'idle', opts.stageTimeout, `stage${index} idle`);
    const receipt = await rpc('session/prompt', { sessionId: opts.session, contentBlocks: [{ type: 'text', text }] });
    stage.messageId = receipt.messageId;
    await running;
    await idle;
    stage.finishedAt = timestamp(); stage.durationMs = Date.now() - begun;
    summary.stages.push(stage);
  }
  ok = true;
} finally {
  summary.finishedAt = timestamp(); summary.ok = ok; summary.notificationCount = notificationCount; summary.finalAssistantEvent = lastAssistant;
  summary.observedRoutes = [...routeEvidence.values()];
  summary.routeViolations = summary.observedRoutes.filter(route =>
    (route.provider !== undefined && route.provider !== 'audit') ||
    (route.model !== undefined && route.model !== 'gpt-5.6-sol'));
  try { summary.shutdown = await rpc('shutdown', undefined, 30); } catch (error) { summary.shutdownError = String(error); }
  await terminate();
  const notificationsFinished = notificationLog.writableFinished ? Promise.resolve() : new Promise(r => notificationLog.once('finish', r));
  const stderrFinished = stderrLog.writableFinished ? Promise.resolve() : new Promise(r => stderrLog.once('finish', r));
  notificationLog.end(); stderrLog.end();
  await Promise.all([notificationsFinished, stderrFinished]);
  await fs.writeFile(resolve(output, 'summary.json'), JSON.stringify(summary, null, 2) + '\n');
}
if (!ok) process.exitCode = 1;
