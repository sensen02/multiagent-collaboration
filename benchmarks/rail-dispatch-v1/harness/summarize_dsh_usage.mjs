#!/usr/bin/env node
import fs from 'node:fs';
import { deriveTurnTokenUsage } from '/home/sensen/.local/node/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-token-meter/lib/types/client.js';

const path=process.argv[2];
if(!path || process.argv.includes('--help')) { console.log('Usage: summarize_dsh_usage.mjs notifications.jsonl'); process.exit(path?0:2); }
const rows=fs.readFileSync(path,'utf8').split(/\n/).filter(Boolean).map(JSON.parse);
const bySession=new Map();
for(const row of rows){ if(row.method!=='session.event' || !row.params?.event) continue; const a=bySession.get(row.params.sessionId)??[]; a.push(row.params.event); bySession.set(row.params.sessionId,a); }
const totals={uncachedInputTokens:0,outputTokens:0,totalTokens:0,cacheReadTokens:0,cacheWriteTokens:0,reasoningTokens:0};
const sessions=[];
for(const [sessionId,events] of bySession){
  const turns=[]; let current=[];
  for(const e of events){
    if(e.type==='turn/start') current=[e];
    else if(current.length){ current.push(e); if(e.type==='turn/end'){ const usage=deriveTurnTokenUsage(current); turns.push({turn:e.data?.turn,reason:e.data?.reason,usage:usage??null}); if(usage) for(const k of Object.keys(totals)) totals[k]+=usage[k]??0; current=[]; } }
  }
  sessions.push({sessionId,turns,eventCount:events.length,incompleteTurn:current.length>0});
}
const routeModels=new Set();
for(const row of rows){ const text=JSON.stringify(row); for(const m of text.matchAll(/\"model\":\"([^\"]+)\"/g)) routeModels.add(m[1]); }
console.log(JSON.stringify({sessionCount:sessions.length,models:[...routeModels].sort(),totals,sessions},null,2));
