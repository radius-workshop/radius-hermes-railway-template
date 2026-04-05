#!/usr/bin/env node
import { formatUnits } from 'viem';
import { initializeWallets } from './wallet-manager.mjs';
import { SBC_DECIMALS } from './radius-chain.mjs';

const walletFlag = process.argv.find((arg) => arg.startsWith('--wallet='));
const selectedWallet = walletFlag ? walletFlag.split('=')[1] : undefined;

const { manager } = await initializeWallets();
const provider = manager.getProvider(selectedWallet);
const address = await provider.getAddress();
const publicClient = provider.getPublicClient();

const [rusdRaw, sbcRaw] = await Promise.all([
  publicClient.getBalance({ address }),
  provider.getSbcBalance(),
]);

console.log(JSON.stringify({
  wallet: provider.name,
  address,
  rusd: formatUnits(rusdRaw, 18),
  rusd_raw: rusdRaw.toString(),
  sbc: formatUnits(sbcRaw, SBC_DECIMALS),
  sbc_raw: sbcRaw.toString(),
}, null, 2));
