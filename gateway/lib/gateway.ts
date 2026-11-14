import { timingSafeEqual } from "node:crypto";
import { tools, instructions, validate, ToolArgumentError } from "./tool-contract";
export interface Services { db: D1Database; bucket: R2Bucket; secret: string; defer?: (task: () => Promise<void>) => void; inlineWrites?: boolean }
type Job = { id: string; owner: string; action: string; digest: string; status: string; created: number; expires: number; lease: string | null; lease_until: number | null; result_key: string | null; input_json: string | null; result_json: string | null };
// Disable writes for a compatible rollback; keep the dual readers and migration.
export const INLINE_TRANSFERS_ENABLED = true;
export const INLINE_TRANSFER_MAX_BYTES = 64 * 1024;
const MAX = 2 * 1024 * 1024;
// Only connector-counted receipts may exceed the conservative legacy byte cap.
// This applies on every public read, including get_operation for pre-upgrade jobs.
export function boundPublicResult(value: Record<string,unknown>): Record<string,unknown> {
  const bytes = new TextEncoder().encode(JSON.stringify(value)).length;
  const meter = value.response_budget as Record<string,unknown>|undefined;
  if (bytes <= 1800 || (bytes <= 32000 && meter?.v === 1 &&
      typeof meter.used_tokens === "number" && Number.isInteger(meter.used_tokens) && meter.used_tokens > 0 && meter.used_tokens <= 2000)) return value;
  const original = value.result as Record<string,unknown>|undefined;
  const base = {operation_id:value.operation_id,status:value.status};
  if(value.ok === true && original?.saved === true) {
    // A committed save must not become a failure merely because its receipt was large.
    const compact: Record<string,unknown> = {saved:true,response_compacted:true,
      note:"Saved successfully. Detailed receipt is retained; retrieve original text through bounded read tools."};
    for(const key of ["sha256","name","version","import_status","has_warnings","imported_with_warnings"])
      if(typeof original[key] === "boolean" || (typeof original[key] === "string" && String(original[key]).length <= 128)) compact[key] = original[key];
    const receipt = original.import_result as Record<string,unknown>|undefined;
    if(receipt) {
      compact.warning_count = Array.isArray(receipt.warnings) ? receipt.warnings.length : 0;
      for(const key of ["added_revisions","reused_messages","repeated_packets"])
        if(Number.isSafeInteger(receipt[key])) compact[key] = receipt[key];
    }
    return {...base,ok:true,result:compact};
  }
  return {...base,ok:false,error:"Stored result exceeds the retrieval budget. Request a fresh bounded search or exact source range.",
    error_detail:{code:"result_too_large",stage:"response",original_result_retained:true}};
}
const COMPLETION_CHECK_MS = 250;
const COMPLETION_WAIT_MS = 12_000;
const MAINTENANCE_PAUSE_SECONDS = 180;
const CLEANUP_INTERVAL_MS = 60_000;
const CLEANUP_BATCH_SIZE = 50;
const cleanupStates = new WeakMap<D1Database,{running:boolean;next:number}>();
const now = () => Math.floor(Date.now() / 1000);
const reply = (data: unknown, status = 200) => Response.json(data, {status, headers:{"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"}});
export class Failure extends Error { constructor(public status: number, message: string) { super(message); } }
class Timing {
  private started = performance.now();
  private phases: Record<string,number> = {};
  async step<T>(phase: string, work: () => Promise<T>): Promise<T> {
    const started = performance.now();
    try { return await work(); }
    finally { this.phases[phase] = (this.phases[phase]??0) + Math.max(0,performance.now()-started); }
  }
  emit(route: "mcp"|"agent", action: string) {
    const durations = Object.fromEntries(Object.entries(this.phases).map(([phase,value])=>[phase,Math.round(value)]));
    console.info(JSON.stringify({component:"threadsatchel",event:"gateway_timing",route,action,...durations,total_ms:Math.max(0,Math.round(performance.now()-this.started))}));
  }
}
function diagnostic(error: unknown) {
  const code = error instanceof ToolArgumentError ? error.code : error instanceof Failure ? ({400:"invalid_request",401:"authentication_required",403:"forbidden",404:"operation_unavailable",409:"request_conflict",413:"size_limit",429:"too_many_pending",503:"temporarily_unavailable"} as Record<number,string>)[error.status] || "request_failed" : "service_unavailable";
  const detail = {code, stage:"gateway", message:error instanceof ToolArgumentError || error instanceof Failure ? error.message : "Request could not be completed; retain the original content and request key.", diagnostic_id:crypto.randomUUID(), timestamp:new Date().toISOString(), ...(error instanceof ToolArgumentError ? {location:error.location,submitted:false} : {})};
  // Metadata only: no packet, request key, authentication, or exception dump.
  console.warn(JSON.stringify({component:"threadsatchel",event:"request_rejected",...detail}));
  return {ok:false,error:detail.message,error_detail:detail};
}
async function body(request: Request) {
  if (Number(request.headers.get("content-length") || 0) > MAX) throw new Failure(413,"Request too large");
  const reader = request.body?.getReader(); if (!reader) throw new Failure(400,"Missing body");
  const chunks: Uint8Array[] = []; let length = 0;
  for (;;) { const {value,done} = await reader.read(); if(done) break; length += value.length; if(length > MAX) { await reader.cancel(); throw new Failure(413,"Request too large"); } chunks.push(value); }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { throw new Failure(400,"Invalid JSON"); }
}
export async function digest(value: string) { return Buffer.from(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(value))).toString("hex"); }
async function owner(id: string | undefined, s: Services, timing: Timing) {
  if(!id) throw new Failure(401,"Authenticate as the configured archive owner");
  // Only the authenticated ingress adapter supplies this argument. Incoming
  // identity headers are untrusted, including on the first request.
  const result = await timing.step("owner_db_ms",()=>s.db.batch<{value:string}>([
    s.db.prepare("INSERT OR IGNORE INTO state(key,value) VALUES('owner',?)").bind(id),
    s.db.prepare("SELECT value FROM state WHERE key='owner'")
  ]));
  const row = result[1].results[0];
  if(row?.value !== id) throw new Failure(403,"This archive belongs to another user");
  return id;
}
function agent(request: Request, s: Services) {
  const supplied = request.headers.get("X-ThreadSatchel-Key") || "";
  const a = Buffer.from(supplied), b = Buffer.from(s.secret);
  if(b.length < 32 || a.length !== b.length || !timingSafeEqual(a,b)) throw new Failure(401,"Unauthorized connector");
}
function connectionStatus(value?: string) {
  const heartbeat = Number(value || 0);
  return {online: now()-heartbeat < 90, last_seen: heartbeat ? new Date(heartbeat*1000).toISOString():null, archive:"local archive", transport:"outbound HTTPS"};
}
export async function status(s: Services) {
  const row = await s.db.prepare("SELECT value FROM state WHERE key='heartbeat'").first<{value:string}>();
  return connectionStatus(row?.value);
}
function inputKey(job: Pick<Job,"id"|"created"|"digest">) { return `${job.id}/input-${job.created}-${job.digest}`; }
function legacyInputKey(job: Pick<Job,"id"|"digest">) { return `${job.id}/input-${job.digest}`; }
function inlineValue(value: string, s: Services) {
  return (s.inlineWrites ?? INLINE_TRANSFERS_ENABLED) && Buffer.byteLength(value,"utf8") <= INLINE_TRANSFER_MAX_BYTES ? value : null;
}
async function cleanup(s: Services) {
  const cutoff = now();
  const rows = await s.db.prepare("SELECT id,digest,created,result_key FROM jobs WHERE expires < ? LIMIT 50").bind(cutoff).all<Pick<Job,"id"|"digest"|"created"|"result_key">>();
  if(rows.results.length) {
    // A newer incarnation of the same request key has a different created time.
    // A delayed sweep may delete these old objects, never the newer input.
    await s.bucket.delete(rows.results.flatMap(row=>[inputKey(row),legacyInputKey(row),...(row.result_key?[row.result_key]:[])]));
    await s.db.batch(rows.results.map(row=>s.db.prepare("DELETE FROM jobs WHERE id=? AND created=? AND expires < ?").bind(row.id,row.created,cutoff)));
  }
  return rows.results.length;
}
async function scheduleCleanup(s: Services) {
  let state = cleanupStates.get(s.db);
  if(!state) { state={running:false,next:0}; cleanupStates.set(s.db,state); }
  if(state.running || performance.now()<state.next) return;
  state.running=true;
  const work = async () => {
    try {
      const count = await cleanup(s);
      // Under continuing traffic, sweep once a minute; a full batch gets another
      // bounded pass on the next request. Expired operations are inaccessible now.
      state.next=performance.now()+(count>=CLEANUP_BATCH_SIZE?0:CLEANUP_INTERVAL_MS);
    } catch {
      state.next=performance.now()+CLEANUP_INTERVAL_MS;
      console.warn(JSON.stringify({component:"threadsatchel",event:"cleanup_failed"}));
    } finally { state.running=false; }
  };
  if(s.defer) {
    try { s.defer(work); return; }
    catch { /* Native/test callers without a request context use bounded cleanup. */ }
  }
  await work();
}
async function completedOperation(id: string, user: string, s: Services) {
  const row = await s.db.prepare("SELECT status,expires,result_key,result_json FROM jobs WHERE id=? AND owner=?").bind(id,user).first<Pick<Job,"status"|"expires"|"result_key"|"result_json">>();
  if(!row || row.expires < now()) throw new Failure(404,"Operation not found or expired; keep original save content and request key");
  if(row.status === "done" && row.result_json != null) {
    return {operation_id:id,status:"completed",...JSON.parse(row.result_json) as Record<string,unknown>};
  }
  if(row.status === "done" && row.result_key) {
    const value = await s.bucket.get(row.result_key); if(!value) throw new Failure(503,"Result temporarily unavailable");
    return {operation_id:id,status:"completed",...await value.json<Record<string,unknown>>()};
  }
  return null;
}
async function pendingOperation(id: string, s: Services) {
  return {operation_id:id,status:"pending",...(await status(s)),message:"Not yet confirmed. Use get_operation with this operation_id; do not create a new save request."};
}
async function operation(id: string, user: string, s: Services) {
  return (await completedOperation(id,user,s)) ?? pendingOperation(id,s);
}
async function waitForOperation(id: string, user: string, s: Services) {
  // Only the public pending response needs a fresh connection status. Internal
  // completion checks perform one owner-scoped job read without a heartbeat read.
  // A monotonic deadline includes DB time instead of adding 12 separate delays.
  // An in-flight storage request cannot be canceled; do not start another check
  // after the wait budget expires.
  const deadline = performance.now() + COMPLETION_WAIT_MS;
  for (;;) {
    const result = await completedOperation(id,user,s);
    if(result) return result;
    const remaining = deadline - performance.now();
    if(remaining <= 0) break;
    await new Promise(resolve=>setTimeout(resolve,Math.min(COMPLETION_CHECK_MS,remaining)));
    if(performance.now() >= deadline) break;
  }
  return pendingOperation(id,s);
}
async function submit(action: string, args: Record<string,unknown>, user: string, s: Services, timing: Timing) {
  const payload = JSON.stringify({action,args}); const hash = await digest(payload);
  const key = typeof args.request_key === "string" ? args.request_key : crypto.randomUUID();
  const id = await digest(`${user}\n${action}\n${key}`);
  const preflight = await timing.step("submit_db_ms",()=>s.db.batch([
    s.db.prepare("SELECT digest,result_key,lease FROM jobs WHERE id=?").bind(id),
    s.db.prepare("SELECT value FROM state WHERE key='heartbeat'"),
    s.db.prepare("SELECT count(*) AS n FROM jobs WHERE owner=? AND status!='done' AND expires >= ?").bind(user,now())
  ]));
  const prior = preflight[0].results[0] as Pick<Job,"digest"|"result_key"|"lease">|undefined;
  if(prior) {
    if(prior.digest !== hash) throw new Failure(409,"Request key already used with different content");
    const previous = await operation(id,user,s);
    if(previous.status !== "completed" || !("ok" in previous) || previous.ok !== false) return previous;
    // Explicitly repeating the same save may retry a failed/uncertain import.
    // Local permanent receipts and the existing importer prevent duplicates.
    if(!(await status(s)).online) return previous;
    // Another retry may already have completed a newer lease while this request
    // was reading the failure. Never erase that newer receipt.
    const reset = await s.db.prepare("UPDATE jobs SET status='pending',lease=NULL,lease_until=NULL,result_key=NULL,result_json=NULL WHERE id=? AND status='done' AND lease IS ? RETURNING id").bind(id,prior.lease).first();
    if(reset && prior.result_key) await s.bucket.delete(prior.result_key);
    return operation(id,user,s);
  }
  if(!connectionStatus((preflight[1].results[0] as {value:string}|undefined)?.value).online) throw new Failure(503,"The archive connector is offline. Nothing was submitted; retry the same content and request key when online.");
  const active = preflight[2].results[0] as {n:number}|undefined;
  if((active?.n || 0)>=32) throw new Failure(429,"Too many pending requests; wait for existing operations");
  const created = now();
  const input = inputKey({id,created,digest:hash});
  const inputJson = inlineValue(payload,s);
  if(inputJson === null) await timing.step("input_put_ms",()=>s.bucket.put(input,payload,{httpMetadata:{contentType:"application/json"}}));
  const inserted = await timing.step("submit_db_ms",()=>s.db.batch<{digest:string;created:number;input_inline:number}>([
    s.db.prepare("INSERT OR IGNORE INTO jobs(id,owner,action,digest,status,created,expires,input_json) VALUES(?,?,?,?,'pending',?,?,?)").bind(id,user,action,hash,created,created+86400,inputJson),
    s.db.prepare("SELECT digest,created,input_json IS NOT NULL AS input_inline FROM jobs WHERE id=?").bind(id)
  ]));
  const stored = inserted[1].results[0];
  // Concurrent identical submissions may have chosen different seconds. Remove
  // only our unused candidate object; never delete the winning generation.
  if(inputJson === null && (!stored || stored.input_inline || input!==inputKey({id,...stored}))) await s.bucket.delete(input);
  if(stored?.digest !== hash) throw new Failure(409,"Request key conflict");
  // Most lookups complete within one poll. Longer operations have durable IDs.
  return timing.step("completion_wait_ms",()=>waitForOperation(id,user,s));
}
export async function handleMcp(request: Request, s: Services, authenticatedOwner?: string): Promise<Response> {
  const timing = new Timing();
  let action: string|null = null;
  try {
    if(request.method !== "POST") return new Response(null,{status:405,headers:{Allow:"POST"}});
    const data = await body(request);
    if(!data || data.jsonrpc !== "2.0" || typeof data.method !== "string") return reply({jsonrpc:"2.0",id:null,error:{code:-32600,message:"Invalid request"}},400);
    const result = (r:unknown) => reply({jsonrpc:"2.0",id:data.id??null,result:r});
    if(data.method === "server/discover") return result({supportedVersions:["2026-07-28"],capabilities:{tools:{}}});
    if(data.method === "initialize") return result({protocolVersion:["2025-03-26","2025-06-18","2025-11-25","2026-07-28"].includes(data.params?.protocolVersion)?data.params.protocolVersion:"2025-03-26",capabilities:{tools:{listChanged:false}},serverInfo:{name:"ThreadSatchel",version:"1.3.0"},instructions});
    if(data.method.startsWith("notifications/")) return new Response(null,{status:202});
    if(data.method === "ping") return result({});
    if(data.method === "tools/list") return result({tools});
    if(data.method !== "tools/call") return reply({jsonrpc:"2.0",id:data.id??null,error:{code:-32601,message:"Method not found"}});
    const user = await owner(authenticatedOwner,s,timing);
    try {
      const name = data.params?.name; const args = validate(name,data.params?.arguments??{});
      action=name;
      await scheduleCleanup(s);
      const value = boundPublicResult(name === "connection_status" ? await timing.step("status_db_ms",()=>status(s)) : name === "get_operation" ? await timing.step("operation_ms",()=>operation(String(args.operation_id),user,s)) : await submit(name,args,user,s,timing));
      return result({content:[{type:"text",text:JSON.stringify(value)}],isError:"ok" in value && value.ok === false});
    } catch(e) { return result({content:[{type:"text",text:JSON.stringify(diagnostic(e))}],isError:true}); }
  } catch(e) { return reply(diagnostic(e),e instanceof Failure?e.status:503); }
  finally { if(action) timing.emit("mcp",action); }
}
export async function handleAgent(request: Request, s: Services): Promise<Response> {
  const timing = new Timing();
  let timedAction: string|null = null;
  try {
    agent(request,s); if(request.method !== "POST") throw new Failure(405,"POST required");
    const data = await body(request);
    // Drain existing work before a connector update. Only authenticated agents
    // can pause claims, and a forgotten gate expires without operator recovery.
    if(data.action === "maintenance") {
      if(!["pause","status","resume"].includes(data.mode)) throw new Failure(400,"Invalid maintenance mode");
      const current = now();
      if(data.mode === "pause") {
        await s.db.prepare("INSERT INTO state(key,value) VALUES('claims_paused_until',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(String(current+MAINTENANCE_PAUSE_SECONDS)).run();
      } else if(data.mode === "resume") {
        await s.db.prepare("DELETE FROM state WHERE key='claims_paused_until'").run();
      }
      const gate = await s.db.prepare("SELECT value FROM state WHERE key='claims_paused_until'").first<{value:string}>();
      const pausedUntil = gate ? Number(gate.value) : null;
      const active = await s.db.prepare("SELECT count(*) AS n FROM jobs WHERE status='leased' AND lease_until>=? AND expires>=?").bind(current,current).first<{n:number}>();
      if((pausedUntil!==null&&(!Number.isSafeInteger(pausedUntil)||pausedUntil<=0)) ||
          !active || !Number.isSafeInteger(active.n) || active.n<0) throw new Failure(503,"Maintenance status unavailable");
      return reply({server_time:current,paused_until:pausedUntil,active_jobs:active.n,paused:pausedUntil!==null&&pausedUntil>=current});
    }
    // A busy connector must remain visible without polling (and claiming) another
    // job. Only the current live lease may renew itself; never revive a stale one.
    if(data.action === "heartbeat") {
      if(typeof data.id!=="string" || !/^[a-f0-9]{64}$/.test(data.id) || typeof data.lease!=="string" || !data.lease || data.lease.length>128) throw new Failure(400,"Invalid heartbeat");
      const current = now();
      const renewed = await s.db.prepare("UPDATE jobs SET lease_until=min(?,expires) WHERE id=? AND lease=? AND status='leased' AND lease_until>=? AND expires>=? RETURNING lease_until").bind(current+300,data.id,data.lease,current,current).first<{lease_until:number}>();
      if(!renewed) {
        const job = await s.db.prepare("SELECT status,lease FROM jobs WHERE id=?").bind(data.id).first<Pick<Job,"status"|"lease">>();
        if(job?.status === "done" && job.lease === data.lease) return reply({acknowledged:true,completed:true});
        throw new Failure(409,"Lease no longer valid");
      }
      await s.db.prepare("INSERT INTO state(key,value) VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(String(current)).run();
      return reply({acknowledged:true,lease_until:renewed.lease_until});
    }
    await scheduleCleanup(s);
    if(data.action === "poll") {
      const token = crypto.randomUUID();
      const current = now();
      // The pause test and claim are one SQL write. A poll already in flight
      // either claimed before pause (and appears in active_jobs) or claims none.
      const claimed = await timing.step("claim_db_ms",()=>s.db.batch<Job>([
        s.db.prepare("INSERT INTO state(key,value) VALUES('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(String(current)),
        s.db.prepare("UPDATE jobs SET status='leased',lease=?,lease_until=? WHERE id=(SELECT id FROM jobs WHERE expires >= ? AND (status='pending' OR (status='leased' AND lease_until < ?)) ORDER BY created LIMIT 1) AND NOT EXISTS(SELECT 1 FROM state WHERE key='claims_paused_until' AND CAST(value AS INTEGER)>=?) RETURNING id,created,digest,lease,input_json").bind(token,current+300,current,current,current)
      ]));
      const job = claimed[1].results[0];
      if(!job) return reply({job:null});
      timedAction="poll";
      const payload = await timing.step("input_get_ms",async()=>{
        if(job.input_json != null) return job.input_json;
        const object = (await s.bucket.get(inputKey(job))) ?? (await s.bucket.get(legacyInputKey(job)));
        if(!object) throw new Failure(503,"Payload unavailable");
        return object.text();
      });
      return reply({job:{id:job.id,lease:job.lease,sha256:job.digest,payload}});
    }
    if(data.action === "complete") {
      if(typeof data.id!=="string" || !/^[a-f0-9]{64}$/.test(data.id) || typeof data.lease!=="string" || typeof data.result!=="object" || data.result===null) throw new Failure(400,"Invalid completion");
      timedAction="complete";
      const job = await timing.step("job_read_ms",()=>s.db.prepare("SELECT id,status,expires,lease,lease_until FROM jobs WHERE id=?").bind(data.id).first<Pick<Job,"id"|"status"|"expires"|"lease"|"lease_until">>());
      if(!job || job.expires<now()) throw new Failure(404,"Operation expired");
      if(job.status === "done") return reply({acknowledged:true});
      if(job.lease!==data.lease || (job.lease_until??0)<now()) throw new Failure(409,"Lease no longer valid");
      const serialized = JSON.stringify(data.result);
      const resultJson = inlineValue(serialized,s);
      // Separate attempts own separate fallback objects. A losing completion
      // must never remove the receipt committed by another completion.
      const key = resultJson === null ? `${job.id}/result-${job.lease}-${crypto.randomUUID()}` : null;
      if(key) await timing.step("result_put_ms",()=>s.bucket.put(key,serialized,{httpMetadata:{contentType:"application/json"}}));
      const current = now();
      const done = await timing.step("commit_db_ms",()=>s.db.prepare("UPDATE jobs SET status='done',result_key=?,result_json=? WHERE id=? AND lease=? AND status='leased' AND lease_until>=? AND expires>=? RETURNING id").bind(key,resultJson,job.id,job.lease,current,current).first());
      if(!done) {
        if(key) await s.bucket.delete(key);
        const committed = await s.db.prepare("SELECT status,lease FROM jobs WHERE id=?").bind(job.id).first<Pick<Job,"status"|"lease">>();
        if(committed?.status === "done" && committed.lease === job.lease) return reply({acknowledged:true});
        throw new Failure(409,"Lease changed");
      }
      return reply({acknowledged:true});
    }
    throw new Failure(400,"Unsupported connector action");
  } catch(e) { return reply({error:e instanceof Failure?e.message:"Connector service unavailable"},e instanceof Failure?e.status:503); }
  finally { if(timedAction) timing.emit("agent",timedAction); }
}
