#!/usr/bin/env node
/**
 * Send SBC tokens on Radius Testnet.
 * Usage: node send.mjs <to_address> <amount_sbc>
 *
 * Reads the private key from RADIUS_PRIVATE_KEY env var
 * or ${HERMES_HOME}/.radius/key.
 *
 * Output (JSON on stdout):
 *   { from, to, amount_sbc, tx_hash, block_number, status }
 */
import { parseUnits, formatUnits, isAddress } from 'viem';
import { readFileSync, existsSync } from 'fs';
import { createClients, SBC_ADDRESS, SBC_DECIMALS, ERC20_ABI } from './chain.mjs';

const HERMES_HOME = process.env.HERMES_HOME || '/data/.hermes';
const KEY_FILE = `${HERMES_HOME}/.radius/key`;

const [, , toArg, amountArg] = process.argv;

if (!toArg || !amountArg) {
  console.error('Usage: node send.mjs <to_address> <amount_sbc>');
  process.exit(1);
}

if (!isAddress(toArg)) {
  console.error(`Invalid address: ${toArg}`);
  process.exit(1);
}

const amountNum = parseFloat(amountArg);
if (isNaN(amountNum) || amountNum <= 0) {
  console.error(`Invalid amount: ${amountArg}`);
  process.exit(1);
}

let privateKey = process.env.RADIUS_PRIVATE_KEY || '';
if (!privateKey && existsSync(KEY_FILE)) {
  privateKey = readFileSync(KEY_FILE, 'utf8').trim();
}
if (!privateKey) {
  console.error('No wallet configured. Set RADIUS_PRIVATE_KEY or run wallet-init.mjs first.');
  process.exit(1);
}

const { publicClient, walletClient, account } = createClients(privateKey);

// Check balance before sending
const balance = await publicClient.readContract({
  address: SBC_ADDRESS,
  abi: ERC20_ABI,
  functionName: 'balanceOf',
  args: [account.address],
});

const amount = parseUnits(amountArg, SBC_DECIMALS);
if (balance < amount) {
  console.error(`Insufficient SBC balance. Have ${formatUnits(balance, SBC_DECIMALS)}, need ${amountArg}.`);
  process.exit(1);
}

console.error(`Sending ${amountArg} SBC from ${account.address} to ${toArg}...`);

const hash = await walletClient.writeContract({
  address: SBC_ADDRESS,
  abi: ERC20_ABI,
  functionName: 'transfer',
  args: [toArg, amount],
});

console.error(`Tx submitted: ${hash}. Waiting for confirmation...`);
const receipt = await publicClient.waitForTransactionReceipt({ hash });

const result = {
  from: account.address,
  to: toArg,
  amount_sbc: amountArg,
  tx_hash: hash,
  block_number: receipt.blockNumber.toString(),
  status: receipt.status,
};
console.log(JSON.stringify(result, null, 2));
