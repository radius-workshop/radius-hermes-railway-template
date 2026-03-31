#!/usr/bin/env node
/**
 * Radius wallet init — runs once on first boot.
 * - Generates a new private key (or uses RADIUS_PRIVATE_KEY if set).
 * - Persists key + address under ${HERMES_HOME}/.radius/.
 * - Requests testnet SBC from the faucet (unless RADIUS_AUTO_FUND=false).
 */
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';
import { createWalletClient, http, formatUnits } from 'viem';
import { readFileSync, writeFileSync, mkdirSync, existsSync, chmodSync } from 'fs';
import { createClients, SBC_ADDRESS, SBC_DECIMALS, ERC20_ABI, FAUCET_BASE, radiusTestnet, RPC_URL } from './chain.mjs';

const HERMES_HOME = process.env.HERMES_HOME || '/data/.hermes';
const RADIUS_DIR = `${HERMES_HOME}/.radius`;
const KEY_FILE = `${RADIUS_DIR}/key`;
const ADDR_FILE = `${RADIUS_DIR}/address`;

mkdirSync(RADIUS_DIR, { recursive: true });

// Determine private key
let privateKey = process.env.RADIUS_PRIVATE_KEY || '';
let isNewKey = false;

if (!privateKey) {
  if (existsSync(KEY_FILE)) {
    privateKey = readFileSync(KEY_FILE, 'utf8').trim();
    console.log('[radius] Using stored wallet key.');
  } else {
    privateKey = generatePrivateKey();
    isNewKey = true;
    console.log('[radius] Generated new wallet private key.');
  }
}

const account = privateKeyToAccount(privateKey);
const address = account.address;
console.log(`[radius] Wallet address: ${address}`);

// Persist key (restricted permissions) and address
if (isNewKey || !existsSync(KEY_FILE)) {
  writeFileSync(KEY_FILE, privateKey, { mode: 0o600 });
  chmodSync(KEY_FILE, 0o600);
}
writeFileSync(ADDR_FILE, address);

// Faucet funding
const autoFund = process.env.RADIUS_AUTO_FUND;
const skipFund = autoFund === 'false' || autoFund === '0';

if (skipFund) {
  console.log('[radius] RADIUS_AUTO_FUND disabled, skipping faucet.');
  process.exit(0);
}

async function getChallenge(addr) {
  const res = await fetch(`${FAUCET_BASE}/challenge/${addr}?token=SBC`);
  if (!res.ok) throw new Error(`Challenge request failed: ${res.status}`);
  const data = await res.json();
  return data.message || data.challenge;
}

async function dripWithSignature(addr) {
  const message = await getChallenge(addr);
  const walletClient = createWalletClient({
    account,
    chain: radiusTestnet,
    transport: http(RPC_URL),
  });
  const signature = await walletClient.signMessage({ message });
  const res = await fetch(`${FAUCET_BASE}/drip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address: addr, token: 'SBC', signature }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.message || JSON.stringify(data));
  return data;
}

async function drip(addr) {
  // Try unsigned first
  const res = await fetch(`${FAUCET_BASE}/drip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address: addr, token: 'SBC' }),
  });
  const data = await res.json();
  if (res.ok) return data;

  const errCode = data.error || '';
  if (errCode === 'signature_required' || res.status === 401) {
    console.log('[radius] Faucet requires signed request, signing challenge...');
    return dripWithSignature(addr);
  }
  if (errCode === 'rate_limited') {
    const retryMs = data.retry_after_ms || data.retry_after_seconds * 1000 || 0;
    console.log(`[radius] Faucet rate-limited. Retry after ${Math.ceil(retryMs / 1000)}s.`);
    return null;
  }
  throw new Error(data.error || data.message || JSON.stringify(data));
}

async function getSbcBalance(addr) {
  const { publicClient } = createClients(privateKey);
  return publicClient.readContract({
    address: SBC_ADDRESS,
    abi: ERC20_ABI,
    functionName: 'balanceOf',
    args: [addr],
  });
}

try {
  console.log('[radius] Requesting SBC from faucet...');
  const result = await drip(address);
  if (result) {
    const txHash = result.tx_hash || result.txHash || result.hash || '';
    if (txHash) console.log(`[radius] Faucet tx: ${txHash}`);
    console.log('[radius] Faucet request submitted. Waiting for balance...');
    // Poll for balance up to 15s
    for (let i = 0; i < 5; i++) {
      await new Promise(r => setTimeout(r, 3000));
      try {
        const bal = await getSbcBalance(address);
        if (bal > 0n) {
          console.log(`[radius] SBC balance: ${formatUnits(bal, SBC_DECIMALS)} SBC`);
          break;
        }
      } catch {
        // RPC might not be ready yet
      }
    }
  }
} catch (err) {
  console.error(`[radius] Faucet funding failed: ${err.message}`);
  console.log('[radius] Continuing — use /radius fund in chat to retry.');
}

console.log('[radius] Wallet initialization complete.');
