// Validate core fields without rebuilding or stripping caller-supplied metadata.
export class ToolArgumentError extends Error {
  constructor(public code: string, message: string, public location = "$") { super(message); }
}
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const nonempty = (value: unknown) => typeof value === "string" && value.trim().length > 0;
export function validatePacket(packet: unknown): asserts packet is Record<string, unknown> {
  const fail = (code: string, message: string, path: string): never => { throw new ToolArgumentError(code, message, path); };
  if (!object(packet)) fail("invalid_packet", "Packet must be an object.", "$.packet");
  const p = packet as Record<string, unknown>;
  const pending: Array<[unknown, number]> = [[p, 0]];
  while (pending.length) {
    const [value, depth] = pending.pop()!;
    if (depth > 100 || typeof value === "number" && !Number.isFinite(value)) fail("invalid_json_value", "Packet metadata must contain finite JSON values with at most 100 nesting levels.", "$.packet");
    if (Array.isArray(value)) { for (const v of value) pending.push([v, depth + 1]); }
    else if (object(value)) for (const v of Object.values(value)) pending.push([v, depth + 1]);
    else if (value !== null && !["string", "number", "boolean"].includes(typeof value)) fail("invalid_json_value", "Packet must contain JSON data only.", "$.packet");
  }
  if (!["threadsatchel/1"].includes(p.format as string)) fail("unsupported_format", "Expected format threadsatchel/1.", "$.packet.format");
  if (!["excerpt", "summary", "note"].includes(p.kind as string)) fail("invalid_kind", "kind must be excerpt, summary, or note.", "$.packet.kind");
  for (const key of ["title", "account_id", "conversation_id", "source_url", "source_date"]) {
    if (p[key] != null && !nonempty(p[key])) fail("invalid_metadata", "A supported metadata field has an invalid value.", "$.packet." + key);
  }
  if ("derived_from" in p && !Array.isArray(p.derived_from)) fail("invalid_metadata", "derived_from must be a list of source references.", "$.packet.derived_from");
  if (!Array.isArray(p.messages) || !p.messages.length) fail("invalid_messages", "messages must be a nonempty list.", "$.packet.messages");
  const ids = new Set(), orders = new Set();
  for (const [index, raw] of (p.messages as unknown[]).entries()) {
    const path = `$.packet.messages[${index}]`;
    if (!object(raw)) fail("invalid_message", "Every message must be an object.", path);
    const m = raw as Record<string, unknown>;
    if (!nonempty(m.text)) fail("invalid_text", "Every message needs nonempty text.", path + ".text");
    for (const key of ["speaker", "message_id", "source_url", "source_date"]) {
      if (m[key] != null && !nonempty(m[key])) fail("invalid_metadata", "A supported metadata field has an invalid value.", path + "." + key);
    }
    if (m.source_order != null && (!Number.isInteger(m.source_order) || (m.source_order as number) < 0)) fail("invalid_source_order", "source_order must be a known nonnegative integer, or omitted.", path + ".source_order");
    for (const [key, seen] of [["message_id", ids], ["source_order", orders]] as const) {
      if (m[key] == null) continue;
      if (seen.has(m[key])) fail("duplicate_identity", "Duplicate message_id or source_order in packet; split revisions into separate packets.", path + "." + key);
      seen.add(m[key]);
    }
  }
  if (Buffer.byteLength(JSON.stringify(p), "utf8") > 1024 * 1024) fail("size_limit", "Memory packet exceeds 1 MiB.", "$.packet");
}
