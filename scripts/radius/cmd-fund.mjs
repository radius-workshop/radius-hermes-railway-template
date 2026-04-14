#!/usr/bin/env node
import { initializeWallets } from './wallet-manager.mjs';

const walletFlag = process.argv.find((arg) => arg.startsWith('--wallet='));
const wallet = walletFlag ? walletFlag.split('=')[1] : undefined;

const { manager } = await initializeWallets({ autoFund: false });

if (wallet === 'all' || wallet === 'both') {
  const out = [];
  for (const name of manager.walletOrder) {
    try {
      out.push({ wallet: name, ...(await manager.getProvider(name).fundFromFaucet()) });
    } catch (error) {
      out.push({ wallet: name, ok: false, message: error instanceof Error ? error.message : String(error) });
    }
  }
  console.log(JSON.stringify({ results: out }, null, 2));
  process.exit(0);
}

const provider = manager.getProvider(wallet);
const result = await provider.fundFromFaucet();
console.log(JSON.stringify({ wallet: provider.name, ...result }, null, 2));
