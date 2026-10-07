// Entry point (Cloudflare only allows handlers here; the logic is in app.js).

import { handleRequest, runCron } from "./app.js";

export default {
  async fetch(request, env) {
    return handleRequest(request, env);
  },
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runCron(env));
  },
};
