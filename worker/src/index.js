// Entry point (Cloudflare only allows handlers here; the logic is in app.js).

import { handleRequest, runCron, runCryptoJob } from "./app.js";
import { CRYPTO_CRON } from "./crypto.js";

export default {
  async fetch(request, env, ctx) {
    return handleRequest(request, env, { waitUntil: (p) => ctx.waitUntil(p) });
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(event.cron === CRYPTO_CRON ? runCryptoJob(env) : runCron(env));
  },
};
