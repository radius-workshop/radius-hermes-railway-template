#!/usr/bin/env node
/**
 * Print the Radius wallet balances (RUSD native + SBC ERC-20).
 * Usage: node balance.mjs [address]
 *
 * If address is omitted, reads from RADIUS_WALLET_ADDRESS env var
 * or ${HERMES_HOME}/.radius/address.
 *
 * Output (JSON on stdout):
 *   { address, rusd, rusd_raw, sbc, sbc_raw }
 */
import { formatUnits } from 'viem';
import { readFileSync, existsSync } from 'fs';
import { createClients, SBC_ADDRESS, SBC_DECIMALS, ERC20_ABI } from './chain.mjs';

const HERMES_HOME = process.env.HERMES_HOME || '/data/.hermes';
const KEY_FILE = `${HERMES_HOME}/.radius/key`;
const ADDR_FILE = `${HERMES_HOME}/.radius/address`;

// Resolve private key (needed to create a public client via createClients)
let privateKey = process.env.RADIUS_PRIVATE_KEY || '';
if (!privateKey && existsSync(KEY_FILE)) {
  privateKey = readFileSync(KEY_FILE, 'utf8').trim();
}
if (!privateKey) {
  console.error('No wallet configured. Set RADIUS_PRIVATE_KEY or run wallet-init.mjs first.');
  process.exit(1);
}

// Resolve address
let address = process.argv[2]
  || process.env.RADIUS_WALLET_ADDRESS
  || (existsSync(ADDR_FILE) ? readFileSync(ADDR_FILE, 'utf8').trim() : '');

if (!address) {
  console.error('No address found. Provide one as an argument or run wallet-init.mjs first.');
  process.exit(1);
}

const { publicClient } = createClients(privateKey);

const [rusdRaw, sbcRaw] = await Promise.all([
  publicClient.getBalance({ address }),
  publicClient.readContract({
    address: SBC_ADDRESS,
    abi: ERC20_ABI,
    functionName: 'balanceOf',
    args: [address],
  }),
]);

const rusd = formatUnits(rusdRaw, 18);
const sbc = formatUnits(sbcRaw, SBC_DECIMALS);

const result = { address, rusd, rusd_raw: rusdRaw.toString(), sbc, sbc_raw: sbcRaw.toString() };
console.log(JSON.stringify(result, null, 2));
