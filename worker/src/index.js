// Entry point (Cloudflare only allows handlers here; the logic is in app.js).

import { handleRequest, runCron, runCryptoJob } from "./app.js";
import { CRYPTO_CRON } from "./crypto.js";

export default {
  async fetch(request, env) {
    return handleRequest(request, env);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(event.cron === CRYPTO_CRON ? runCryptoJob(env) : runCron(env));
  },
};
