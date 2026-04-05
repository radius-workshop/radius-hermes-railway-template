#!/usr/bin/env node
import { isAddress } from 'viem';
import { initializeWallets } from './wallet-manager.mjs';
import { txExplorerUrl } from './radius-chain.mjs';

const args = process.argv.slice(2);
const walletArg = args.find((a) => a.startsWith('--wallet='));
const wallet = walletArg ? walletArg.split('=')[1] : undefined;
const positional = args.filter((a) => !a.startsWith('--wallet='));

const [to, amount] = positional;
if (!to || !amount) {
  console.error('Usage: node cmd-send.mjs [--wallet=local|para] <to_address> <amount_sbc>');
  process.exit(1);
}
if (!isAddress(to)) {
  console.error(`Invalid address: ${to}`);
  process.exit(1);
}

const { manager } = await initializeWallets({ autoFund: false });
const provider = manager.getProvider(wallet);

const txHash = await provider.sendTransaction({ to, amountSbc: amount });

console.log(JSON.stringify({
  wallet: provider.name,
  from: await provider.getAddress(),
  to,
  amount_sbc: amount,
  tx_hash: txHash,
  explorer: txExplorerUrl(txHash),
}, null, 2));
