import { timingSafeEqual } from 'node:crypto';
import { handleAgent, handleMcp, type Services } from './lib/gateway';

interface Env {
  DB: D1Database;
  BUCKET: R2Bucket;
  OWNER_ID: string;
  OWNER_TOKEN: string;
  CONNECTOR_TOKEN: string;
  CONNECTOR_KEY: string;
}

function authorized(request: Request, token: string): boolean {
  const provided = Buffer.from(request.headers.get('Authorization') || '');
  const expected = Buffer.from('Bearer ' + token);
  return provided.length === expected.length && timingSafeEqual(provided, expected);
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (path !== '/mcp' && path !== '/api/agent') return new Response(null, {status: 404});
    const secrets = [env.OWNER_TOKEN, env.CONNECTOR_TOKEN, env.CONNECTOR_KEY];
    if (!env.DB || !env.BUCKET || !env.OWNER_ID?.trim() || new Set(secrets).size !== 3 ||
        secrets.some(secret => typeof secret !== 'string' || secret.length < 32)) {
      return new Response('Configure the gateway bindings and three distinct secrets.', {status: 503});
    }
    if (!authorized(request, path === '/mcp' ? env.OWNER_TOKEN : env.CONNECTOR_TOKEN)) {
      return new Response('Authentication required', {status: 401, headers: {
        'WWW-Authenticate': 'Bearer realm="ThreadSatchel"', 'Cache-Control': 'no-store'
      }});
    }
    const services: Services = {db: env.DB, bucket: env.BUCKET, secret: env.CONNECTOR_KEY,
      defer: task => ctx.waitUntil(task())};
    return path === '/mcp'
      ? handleMcp(request, services, env.OWNER_ID)
      : handleAgent(request, services);
  }
};
