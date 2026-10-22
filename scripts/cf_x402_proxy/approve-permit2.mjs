#!/usr/bin/env node
// One-time setup for Cloudflare x402 inference: inspect the paying wallet on Base and grant
// Permit2 a BOUNDED USDC allowance (never unlimited). The allowance is also a lifetime spend ceiling:
// USDC lowers it by every amount Permit2 moves, so payments stop when it runs out.
//
//   node approve-permit2.mjs status
//   node approve-permit2.mjs approve 5          # prints the plan only
//   node approve-permit2.mjs approve 5 --yes    # sends approve(Permit2, 5 USDC) on Base (needs Base ETH for gas)
//   node approve-permit2.mjs approve 0 --yes    # revoke
//
// Key: CF_X402_PRIVATE_KEY, else RADIUS_PRIVATE_KEY. RPC: CF_X402_BASE_RPC_URL, else https://mainnet.base.org.
import { createRadiusFetch } from 'radius-sdk/client';
import { createPublicClient, formatEther, http } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { PERMIT2_ADDRESS, atomicToUsdc } from './lib.mjs';

const MAX_APPROVAL_USDC = 1000;
const [cmd, amountArg, ...rest] = process.argv.slice(2);
const yes = rest.includes('--yes');

const key = (process.env.CF_X402_PRIVATE_KEY || process.env.RADIUS_PRIVATE_KEY || '').trim();
if (!/^0x[0-9a-fA-F]{64}$/.test(key)) {
  console.error('Set CF_X402_PRIVATE_KEY (or RADIUS_PRIVATE_KEY) to the paying wallet key.');
  process.exit(2);
}
const rpcUrl = process.env.CF_X402_BASE_RPC_URL?.trim() || undefined;
let approved = false;
const wallet = createRadiusFetch({
  network: 'base',
  ...(rpcUrl ? { rpcUrl } : {}),
  protocols: ['x402'],
  signer: key,
  maxPerRequest: { amount: '0' },
  permit2Approval: 'never',
  onApprovalRequired: (req) => approved && req.reason === 'approve' && req.spender.toLowerCase() === PERMIT2_ADDRESS.toLowerCase(),
});
const address = privateKeyToAccount(key).address;
const pub = createPublicClient({ chain: wallet.network.chain, transport: http(wallet.network.rpcUrl) });

async function status() {
  const [eth, usdc, allowance] = await Promise.all([pub.getBalance({ address }), wallet.balance(), wallet.permit2Allowance()]);
  console.log(`wallet            ${address}`);
  console.log(`network           Base (eip155:8453) via ${new URL(wallet.network.rpcUrl).host}`);
  console.log(`ETH (gas)         ${formatEther(eth)}`);
  console.log(`USDC              ${usdc.formatted}`);
  console.log(`Permit2 allowance ${allowance > 10n ** 30n ? 'UNLIMITED (consider revoking: approve 0 --yes, then a bounded amount)' : `${atomicToUsdc(allowance)} USDC`}`);
}

async function main() {
  if (cmd === 'status') {
    await status();
  } else if (cmd === 'approve') {
    if (!/^\d+(\.\d{1,6})?$/.test(amountArg ?? '') || Number(amountArg) > MAX_APPROVAL_USDC) {
      console.error(`approve takes a USDC amount between 0 and ${MAX_APPROVAL_USDC} with at most 6 decimals (unlimited approvals are not offered).`);
      process.exit(2);
    }
    await status();
    console.log(`\nPlan: USDC.approve(Permit2 ${PERMIT2_ADDRESS}, ${amountArg} USDC) on Base, from ${address}. Gas is paid in Base ETH.`);
    if (!yes) {
      console.log('Dry run. Re-run with --yes to send the transaction.');
      process.exit(0);
    }
    approved = true;
    const tx = await wallet.approve(PERMIT2_ADDRESS, amountArg);
    console.log(`approval ${tx.status}: ${tx.explorerUrl ?? tx.hash}`);
    await status();
    process.exit(tx.status === 'success' ? 0 : 1);
  } else {
    console.error('usage: approve-permit2.mjs status | approve <usdc> [--yes]');
    process.exit(2);
  }
}

main().catch((e) => {
  console.error(`error: ${e?.shortMessage ?? e?.message ?? e}`);
  process.exit(1);
});
