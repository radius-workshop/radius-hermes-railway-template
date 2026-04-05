#!/usr/bin/env node
import { formatUnits, isAddress } from 'viem';
import { initializeWallets } from './wallet-manager.mjs';
import { SBC_DECIMALS, SBC_ADDRESS, ERC20_ABI } from './radius-chain.mjs';

const walletFlag = process.argv.find((arg) => arg.startsWith('--wallet='));
const selectedWallet = walletFlag ? walletFlag.split('=')[1] : undefined;
const positional = process.argv.slice(2).filter((arg) => !arg.startsWith('--wallet='));
const targetAddress = positional[0];

if (targetAddress && !isAddress(targetAddress)) {
  console.error(`Invalid address: ${targetAddress}`);
  process.exit(1);
}

const { manager } = await initializeWallets({ autoFund: false });
const provider = manager.getProvider(selectedWallet);
const address = targetAddress || await provider.getAddress();
const publicClient = provider.getPublicClient();

const [rusdRaw, sbcRaw] = await Promise.all([
  publicClient.getBalance({ address }),
  publicClient.readContract({
    address: SBC_ADDRESS,
    abi: ERC20_ABI,
    functionName: 'balanceOf',
    args: [address],
  }),
]);

console.log(JSON.stringify({
  wallet: provider.name,
  address,
  rusd: formatUnits(rusdRaw, 18),
  rusd_raw: rusdRaw.toString(),
  sbc: formatUnits(sbcRaw, SBC_DECIMALS),
  sbc_raw: sbcRaw.toString(),
}, null, 2));
