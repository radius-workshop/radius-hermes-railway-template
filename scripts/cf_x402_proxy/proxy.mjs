#!/usr/bin/env node
// Local OpenAI-compatible endpoint for Hermes that pays Cloudflare AI Gateway inference per call
// over x402 (Base USDC). Started by entrypoint.sh when RADIUS_INFERENCE_PROVIDER=cloudflare-x402.
// Listens on 127.0.0.1 only.
import { createServer } from 'node:http';
import { Budget, ConfigError, atomicToUsdc, createHandler, createPayer, loadConfig, spentFromLedger } from './lib.mjs';

let config;
try {
  config = loadConfig();
} catch (e) {
  if (e instanceof ConfigError) {
    console.error(`[cf-x402] config error: ${e.message}`);
    process.exit(2);
  }
  throw e;
}

if (process.argv.includes('--check')) {
  console.log(`[cf-x402] configuration ok: model=${config.model} max_per_request=${atomicToUsdc(config.maxPerRequest)} USDC daily_budget=${atomicToUsdc(config.dailyBudget)} USDC`);
  process.exit(0);
}

const budget = new Budget(config.dailyBudget, spentFromLedger(config.ledgerPath));
const payer = createPayer(config);
const handler = createHandler(config, { payer, budget });

const server = createServer((req, res) => {
  handler(req, res).catch((e) => {
    console.error(`[cf-x402] internal error: ${e?.name ?? 'Error'}`);
    if (!res.headersSent) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'internal proxy error', type: 'internal_error', code: 'internal_error' } }));
    } else {
      res.destroy();
    }
  });
});
server.requestTimeout = config.timeoutMs + 30_000;
server.listen(config.port, '127.0.0.1', () => {
  console.log(
    `[cf-x402] listening on http://127.0.0.1:${config.port}/v1 model=${config.model} payer=${payer.address} network=eip155:8453 ` +
      `max_per_request=${atomicToUsdc(config.maxPerRequest)} USDC daily_budget=${atomicToUsdc(config.dailyBudget)} USDC ` +
      `(used ${atomicToUsdc(budget.used())} in the last 24h) max_tokens=${config.maxTokens}`,
  );
});
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    server.close(() => process.exit(0));
    server.closeIdleConnections();
    setTimeout(() => process.exit(0), 5000).unref();
  });
}
