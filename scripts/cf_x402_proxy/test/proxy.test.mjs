// End-to-end tests against a local mock of Cloudflare's x402 `/ai/run` and a mock Base JSON-RPC.
// Throwaway keys only; nothing touches a real chain or Cloudflare.
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';
import { verifyTypedData, encodeAbiParameters } from 'viem';
import { uptoPermit2WitnessTypes } from '@x402/evm';
import {
  BASE_USDC, PERMIT2_ADDRESS, UPTO_PERMIT2_PROXY, Budget, appendLedger, ConfigError, createHandler, createPayer, guardedSigner,
  loadConfig, spentFromLedger, toChatCompletion, toRunBody,
} from '../lib.mjs';

const ACCOUNT_ID = '0123456789abcdef0123456789abcdef';
const CF_TOKEN = 'cf-test-token-should-never-be-logged';
const PROXY_KEY = 'local-proxy-key-0123456789abcdef0123456789';
const PAY_TO = '0x1111111111111111111111111111111111111111';
const FACILITATOR = '0x2222222222222222222222222222222222222222';
const TX = `0x${'ab'.repeat(32)}`;

function listen(handler) {
  return new Promise((resolve) => {
    const server = createServer(handler);
    server.listen(0, '127.0.0.1', () => resolve({ server, url: `http://127.0.0.1:${server.address().port}` }));
  });
}

async function json(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || 'null');
}

/** Mock Cloudflare /ai/run (x402 v2 upto) + Base JSON-RPC on one server. */
function mockCloudflare(state) {
  return async (req, res) => {
    if (req.url === '/rpc') {
      const body = await json(req);
      const one = (call) => {
        if (call.method === 'eth_chainId') return { jsonrpc: '2.0', id: call.id, result: '0x2105' };
        if (call.method === 'eth_call') {
          state.rpcCalls++;
          if (state.rpcDown) return { jsonrpc: '2.0', id: call.id, error: { code: -32000, message: 'down' } };
          return { jsonrpc: '2.0', id: call.id, result: encodeAbiParameters([{ type: 'uint256' }], [state.allowance]) };
        }
        return { jsonrpc: '2.0', id: call.id, error: { code: -32601, message: 'unsupported' } };
      };
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify(Array.isArray(body) ? body.map(one) : one(body)));
    }
    if (req.method !== 'POST' || req.url !== `/client/v4/accounts/${ACCOUNT_ID}/ai/run`) {
      res.writeHead(404);
      return res.end();
    }
    const body = await json(req);
    state.requests.push({ headers: req.headers, body });
    assert.equal(req.headers.authorization, `Bearer ${CF_TOKEN}`);
    if (state.mode === 'no402') {
      res.writeHead(200, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ success: true, result: { response: 'billed to account' } }));
    }
    assert.equal(req.headers['payment-method'], 'x402');
    const requirement = {
      scheme: 'upto', network: 'eip155:8453', amount: state.max, asset: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', payTo: PAY_TO,
      maxTimeoutSeconds: 300, extra: { name: 'USD Coin', version: '2', facilitatorAddress: FACILITATOR },
    };
    const sig = req.headers['payment-signature'];
    if (!sig) {
      const challenge = { x402Version: 2, error: 'PAYMENT-SIGNATURE header is required', resource: { url: 'https://api.cloudflare.com/ai/run' }, accepts: [requirement],
        ...(state.extensions ? { extensions: state.extensions } : {}) };
      res.writeHead(402, { 'payment-required': Buffer.from(JSON.stringify(challenge)).toString('base64'), 'content-type': 'application/json' });
      return res.end('{}');
    }
    const payload = JSON.parse(Buffer.from(sig, 'base64').toString('utf8'));
    const auth = payload.payload.permit2Authorization;
    const valid = await verifyTypedData({
      address: auth.from,
      domain: { name: 'Permit2', chainId: 8453, verifyingContract: PERMIT2_ADDRESS },
      types: uptoPermit2WitnessTypes,
      primaryType: 'PermitWitnessTransferFrom',
      message: {
        permitted: { token: auth.permitted.token, amount: BigInt(auth.permitted.amount) },
        spender: auth.spender, nonce: BigInt(auth.nonce), deadline: BigInt(auth.deadline),
        witness: { to: auth.witness.to, facilitator: auth.witness.facilitator, validAfter: BigInt(auth.witness.validAfter) },
      },
      signature: payload.payload.signature,
    });
    state.paid.push({ payload, valid });
    assert.equal(valid, true);
    assert.equal(auth.spender, UPTO_PERMIT2_PROXY);
    assert.equal(auth.permitted.amount, state.max);
    assert.equal(auth.witness.to.toLowerCase(), PAY_TO);
    assert.equal(auth.witness.facilitator.toLowerCase(), FACILITATOR);
    // x402 echoes the server's declared extensions; a signed permit/approval would add `info`.
    for (const ext of Object.values(payload.extensions ?? {})) assert.equal(ext?.info, undefined, 'no gas-sponsoring permit or approval may be signed');
    if (state.mode === 'reject') {
      res.writeHead(402, { 'payment-required': Buffer.from(JSON.stringify({ x402Version: 2, error: 'insufficient_funds', accepts: [requirement] })).toString('base64') });
      return res.end('{}');
    }
    if (state.mode === 'hang') return; // never answers; the proxy times out after sending the payment
    const receipt = { success: true, transaction: TX, network: 'eip155:8453', payer: auth.from, amount: state.settle };
    const headers = { 'content-type': 'application/json' };
    if (state.mode !== 'noReceipt') headers['payment-response'] = Buffer.from(JSON.stringify(receipt)).toString('base64');
    res.writeHead(200, headers);
    const result = state.mode === 'native'
      ? { response: null, tool_calls: [{ name: 'terminal', arguments: { command: 'ls' } }], usage: { prompt_tokens: 10, completion_tokens: 3, total_tokens: 13 } }
      : { id: 'chatcmpl-x', object: 'chat.completion', model: 'z-ai/glm-4.7-flash', choices: [{ index: 0, message: { role: 'assistant', content: 'Cloudflare is a network.' }, finish_reason: 'stop' }],
        usage: { prompt_tokens: 12, completion_tokens: 5, total_tokens: 17 } };
    await new Promise((r) => setTimeout(r, state.delayMs ?? 0));
    res.end(JSON.stringify({ success: true, errors: [], messages: [], result }));
  };
}

let cf;
let proxy;
let state;
let logs;
let dir;
let privateKey;
let config;

async function start(overrides = {}) {
  config = loadConfig({
    CLOUDFLARE_ACCOUNT_ID: ACCOUNT_ID, CLOUDFLARE_API_TOKEN: CF_TOKEN, CF_X402_PRIVATE_KEY: privateKey, CF_X402_PROXY_KEY: PROXY_KEY,
    CF_X402_API_BASE: `${cf.url}/client/v4`, CF_X402_BASE_RPC_URL: `${cf.url}/rpc`, HERMES_HOME: dir, ...overrides,
  });
  const budget = new Budget(config.dailyBudget, spentFromLedger(config.ledgerPath));
  const handler = createHandler(config, { payer: createPayer(config), budget, log: (l) => logs.push(l) });
  proxy = await listen((req, res) => handler(req, res).catch((e) => { res.writeHead(500); res.end(String(e)); }));
}

function chat(body, key = PROXY_KEY) {
  return fetch(`${proxy.url}/v1/chat/completions`, {
    method: 'POST', headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json' },
    body: JSON.stringify({ model: 'ignored', messages: [{ role: 'user', content: 'What is Cloudflare?' }], ...body }),
  });
}

function ledgerRows() {
  const p = join(dir, 'x402', 'ledger.jsonl');
  return existsSync(p) ? readFileSync(p, 'utf8').trim().split('\n').map((l) => JSON.parse(l)) : [];
}

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), 'cf-x402-'));
  privateKey = generatePrivateKey();
  logs = [];
  state = { mode: 'ok', max: '50000', settle: '1000', allowance: 5_000_000n, requests: [], paid: [], rpcCalls: 0 };
  cf = await listen(mockCloudflare(state));
});

afterEach(async () => {
  proxy?.server.closeAllConnections?.();
  cf?.server.closeAllConnections?.();
  await Promise.all([proxy && new Promise((r) => proxy.server.close(r)), new Promise((r) => cf.server.close(r))]);
  proxy = undefined;
});

test('402 upto -> signed Permit2 retry -> 200 with settlement; ledger records the settled amount', async () => {
  await start();
  const res = await chat({ max_tokens: 100 });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.object, 'chat.completion');
  assert.equal(body.choices[0].message.content, 'Cloudflare is a network.');
  assert.equal(state.paid.length, 1);
  // The model and max_tokens are the operator's; the payment header is only on the retry.
  assert.equal(state.requests[0].body.model, 'z-ai/glm-4.7-flash');
  assert.equal(state.requests[0].body.input.max_tokens, 100);
  assert.equal(state.requests[0].headers['payment-signature'], undefined);
  const rows = ledgerRows();
  assert.deepEqual(rows.map((r) => r.event), ['started', 'signing', 'completed']);
  // Written before signing, so a crash mid-payment is still charged against the budget at the max.
  assert.equal(rows[1].outcome, 'payment_unknown');
  assert.equal(rows[1].authorized_max_atomic, '50000');
  assert.equal(spentFromLedger(join(dir, 'x402', 'ledger.jsonl')).length, 1);
  const done = rows[2];
  assert.equal(done.outcome, 'settlement_reported');
  assert.equal(done.settled_atomic, '1000');
  assert.equal(done.amount, '0.001');
  assert.equal(done.authorized_max_atomic, '50000');
  assert.equal(done.tx_hash, TX);
  assert.equal(done.requested_by, 'cf-x402-proxy');
  assert.equal(done.accepts[0].scheme, 'upto');
  assert.ok(!done.url.includes(ACCOUNT_ID));
});

test('stream:true gets an OpenAI SSE stream synthesised from the paid (non-streamed) response', async () => {
  await start();
  const res = await chat({ stream: true });
  assert.equal(res.headers.get('content-type'), 'text/event-stream');
  const text = await res.text();
  assert.equal(state.requests[0].body.input.stream, undefined);
  const events = text.trim().split('\n\n').map((e) => e.replace(/^data: /, ''));
  assert.equal(events.at(-1), '[DONE]');
  const chunks = events.slice(0, -1).map((e) => JSON.parse(e));
  assert.equal(chunks[0].choices[0].delta.content, 'Cloudflare is a network.');
  assert.equal(chunks[1].choices[0].finish_reason, 'stop');
  assert.equal(chunks[2].usage.total_tokens, 17);
});

test('Workers AI native tool calls become OpenAI tool_calls with string arguments', async () => {
  state.mode = 'native';
  await start();
  const body = await (await chat({ tools: [{ type: 'function', function: { name: 'terminal', parameters: { type: 'object' } } }] })).json();
  const choice = body.choices[0];
  assert.equal(choice.finish_reason, 'tool_calls');
  assert.equal(choice.message.tool_calls[0].function.name, 'terminal');
  assert.equal(choice.message.tool_calls[0].function.arguments, '{"command":"ls"}');
  assert.equal(state.requests[0].body.input.tools[0].function.name, 'terminal');
});

test('an offer above CF_X402_MAX_PER_REQUEST is refused before signing', async () => {
  state.max = '200000';
  await start({ CF_X402_MAX_PER_REQUEST: '0.1' });
  const res = await chat({});
  assert.equal(res.status, 402);
  assert.equal(state.paid.length, 0);
  assert.equal(ledgerRows().at(-1).outcome, 'refused');
});

test('a Permit2 allowance below the offer is refused (no approval, no signature)', async () => {
  state.allowance = 10_000n;
  state.extensions = { eip2612GasSponsoring: {}, erc20ApprovalGasSponsoring: {} };
  await start();
  const res = await chat({});
  assert.equal(res.status, 402);
  assert.equal((await res.json()).error.code, 'approval_required');
  assert.equal(state.paid.length, 0);
});

test('an RPC failure on the allowance read fails closed', async () => {
  state.rpcDown = true;
  await start();
  const res = await chat({});
  assert.equal(res.status, 402);
  assert.equal(state.paid.length, 0);
});

test('with sufficient allowance a 402 advertising gas sponsoring is paid without signing any extension', async () => {
  state.extensions = { eip2612GasSponsoring: {}, erc20ApprovalGasSponsoring: {} };
  await start();
  assert.equal((await chat({})).status, 200);
  assert.equal(state.paid.length, 1); // the mock asserts payload.extensions is undefined
});

test('the rolling daily budget counts authorised maxima in flight and settled amounts after', async () => {
  state.max = '60000';
  state.settle = '60000';
  await start({ CF_X402_DAILY_BUDGET: '0.1', CF_X402_MAX_PER_REQUEST: '0.1' });
  assert.equal((await chat({ messages: [{ role: 'user', content: 'one' }] })).status, 200);
  const res = await chat({ messages: [{ role: 'user', content: 'two' }] });
  assert.equal(res.status, 402);
  assert.equal((await res.json()).error.code, 'budget_exceeded');
  assert.equal(state.paid.length, 1);
});

test('the budget survives a restart through the ledger', async () => {
  state.max = '60000';
  state.settle = '60000';
  await start({ CF_X402_DAILY_BUDGET: '0.1', CF_X402_MAX_PER_REQUEST: '0.1' });
  assert.equal((await chat({ messages: [{ role: 'user', content: 'one' }] })).status, 200);
  assert.equal(spentFromLedger(join(dir, 'x402', 'ledger.jsonl')).reduce((s, e) => s + e.atomic, 0n), 60000n);
});

test('refusals cost nothing after a restart; a crash after the signing row counts the maximum', async () => {
  state.max = '200000';
  await start({ CF_X402_MAX_PER_REQUEST: '0.1' });
  assert.equal((await chat({})).status, 402);
  state.allowance = 0n;
  state.max = '50000';
  assert.equal((await chat({ messages: [{ role: 'user', content: 'b' }] })).status, 402);
  const path = join(dir, 'x402', 'ledger.jsonl');
  assert.deepEqual(spentFromLedger(path), []);
  // Simulate a crash: only `started` and `signing` rows for an id.
  const crashed = { id: 'crash', ts: new Date().toISOString(), requested_by: 'cf-x402-proxy' };
  appendLedger(path, { ...crashed, event: 'started', outcome: 'pending' });
  appendLedger(path, { ...crashed, event: 'signing', outcome: 'payment_unknown', authorized_max_atomic: '50000' });
  assert.deepEqual(spentFromLedger(path).map((e) => e.atomic), [50000n]);
});

test('a 2xx without a 402 (billed to the Cloudflare account) is delivered once, then the proxy refuses', async () => {
  state.mode = 'no402';
  await start();
  assert.equal((await chat({})).status, 200);
  assert.equal(ledgerRows().at(-1).outcome, 'no_payment_required');
  const second = await chat({ messages: [{ role: 'user', content: 'again' }] });
  assert.equal(second.status, 503);
  assert.equal(state.requests.length, 1);
  assert.equal((await fetch(`${proxy.url}/health`)).status, 503);
});

test('identical concurrent requests and an immediate retry pay once', async () => {
  state.delayMs = 150;
  await start();
  const [a, b] = await Promise.all([chat({}), chat({})]);
  assert.equal(a.status, 200);
  assert.equal(b.status, 200);
  assert.equal((await chat({})).status, 200);
  assert.equal(state.paid.length, 1);
});

test('a timeout after the payment was sent is payment_unknown and is not retried', async () => {
  state.mode = 'hang';
  await start({ CF_X402_TIMEOUT_SECONDS: '1' });
  const res = await chat({});
  assert.equal(res.status, 502);
  assert.equal((await res.json()).error.code, 'payment_unknown');
  const row = ledgerRows().at(-1);
  assert.equal(row.outcome, 'payment_unknown');
  assert.equal(row.authorized_max_atomic, '50000');
  assert.equal((await chat({})).status, 502);
  assert.equal(state.paid.length, 1);
});

test('a missing settlement header delivers the answer but records payment_unknown at the authorised max', async () => {
  state.mode = 'noReceipt';
  await start();
  assert.equal((await chat({})).status, 200);
  const row = ledgerRows().at(-1);
  assert.equal(row.outcome, 'payment_unknown');
  assert.equal(spentFromLedger(join(dir, 'x402', 'ledger.jsonl'))[0].atomic, 50000n);
});

test('a rejected payment (second 402) settles nothing and releases the reservation', async () => {
  state.mode = 'reject';
  await start({ CF_X402_DAILY_BUDGET: '0.06', CF_X402_MAX_PER_REQUEST: '0.06' });
  const res = await chat({});
  assert.equal(res.status, 402);
  assert.equal((await res.json()).error.code, 'payment_rejected');
  state.mode = 'ok';
  assert.equal((await chat({ messages: [{ role: 'user', content: 'next' }] })).status, 200);
});

test('wrong local key is rejected; /v1/models lists the configured model', async () => {
  await start({ CF_X402_CONTEXT_LENGTH: '65536' });
  assert.equal((await chat({}, 'wrong')).status, 401);
  const models = await (await fetch(`${proxy.url}/v1/models`, { headers: { authorization: `Bearer ${PROXY_KEY}` } })).json();
  assert.deepEqual(models.data.map((m) => [m.id, m.context_length]), [['z-ai/glm-4.7-flash', 65536]]);
  assert.equal(state.requests.length, 0);
});

test('secrets, signatures and prompts never reach logs or the ledger', async () => {
  await start();
  await chat({ messages: [{ role: 'user', content: 'top secret prompt' }] });
  const sig = state.requests[1].headers['payment-signature'];
  const sinks = logs.join('\n') + readFileSync(join(dir, 'x402', 'ledger.jsonl'), 'utf8');
  for (const secret of [privateKey.slice(2), CF_TOKEN, PROXY_KEY, sig, state.paid[0].payload.payload.signature, 'top secret prompt', ACCOUNT_ID]) {
    assert.ok(!sinks.includes(secret), `leaked: ${secret.slice(0, 12)}...`);
  }
});

test('the signer refuses anything but a capped payment authorisation', async () => {
  const account = privateKeyToAccount(generatePrivateKey());
  const signer = guardedSigner(account, 50000n);
  const permitted = { token: BASE_USDC, amount: 50000n };
  const witness = { primaryType: 'PermitWitnessTransferFrom', domain: { name: 'Permit2', chainId: 8453, verifyingContract: PERMIT2_ADDRESS }, types: uptoPermit2WitnessTypes,
    message: { permitted, spender: UPTO_PERMIT2_PROXY, nonce: 1n, deadline: 2n, witness: { to: PAY_TO, facilitator: FACILITATOR, validAfter: 0n } } };
  assert.match(await signer.signTypedData(witness), /^0x/);
  await assert.rejects(signer.signTypedData({ ...witness, message: { ...witness.message, permitted: { ...permitted, amount: 50001n } } }), /out of policy/);
  await assert.rejects(signer.signTypedData({ ...witness, message: { ...witness.message, spender: PAY_TO } }), /spender/);
  await assert.rejects(signer.signTypedData({ primaryType: 'Permit', domain: { name: 'USD Coin', chainId: 8453, verifyingContract: BASE_USDC }, message: {} }), /refusing to sign typed data Permit/);
});

test('config validation', () => {
  const base = { CLOUDFLARE_ACCOUNT_ID: ACCOUNT_ID, CLOUDFLARE_API_TOKEN: 't', RADIUS_PRIVATE_KEY: generatePrivateKey(), CF_X402_PROXY_KEY: PROXY_KEY };
  assert.equal(loadConfig(base).runUrl, `https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/ai/run`);
  assert.equal(loadConfig(base).maxPerRequest, 150000n);
  assert.throws(() => loadConfig({ ...base, CLOUDFLARE_ACCOUNT_ID: 'x' }), ConfigError);
  assert.throws(() => loadConfig({ ...base, RADIUS_PRIVATE_KEY: '' }), ConfigError);
  assert.throws(() => loadConfig({ ...base, CF_X402_API_BASE: 'http://example.com' }), ConfigError);
  assert.throws(() => loadConfig({ ...base, CF_X402_MAX_PER_REQUEST: '5', CF_X402_DAILY_BUDGET: '1' }), ConfigError);
  assert.throws(() => loadConfig({ ...base, CF_X402_MAX_PER_REQUEST: '0.0000001' }), ConfigError);
});

test('request translation forwards OpenAI fields into input and clamps max_tokens', () => {
  const body = toRunBody({ model: 'x', stream: true, max_completion_tokens: 999999, temperature: 0.2, user: 'u', messages: [{ role: 'user', content: 'hi' }] }, { model: 'm', maxTokens: 8192 });
  assert.deepEqual(body, { model: 'm', input: { messages: [{ role: 'user', content: 'hi' }], temperature: 0.2, max_tokens: 8192 } });
  assert.equal(toChatCompletion({ result: { foo: 1 } }, 'm'), undefined);
});
