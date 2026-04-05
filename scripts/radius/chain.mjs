import { createWalletClient, http } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { radiusTestnet, RPC_URL, SBC_ADDRESS, SBC_DECIMALS, ERC20_ABI, FAUCET_BASE, getPublicClient } from './radius-chain.mjs';

export { radiusTestnet, RPC_URL, SBC_ADDRESS, SBC_DECIMALS, ERC20_ABI, FAUCET_BASE };

export function createClients(privateKey) {
  const account = privateKeyToAccount(privateKey);
  const transport = http(RPC_URL);
  const publicClient = getPublicClient();
  const walletClient = createWalletClient({ account, chain: radiusTestnet, transport });
  return { publicClient, walletClient, account };
}
