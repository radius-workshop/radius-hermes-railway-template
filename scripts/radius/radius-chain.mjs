import { defineChain, createPublicClient, http } from 'viem';

export const radiusTestnet = defineChain({
  id: 72344,
  name: 'Radius Testnet',
  nativeCurrency: { name: 'RUSD', symbol: 'RUSD', decimals: 18 },
  rpcUrls: {
    default: { http: ['https://rpc.testnet.radiustech.xyz'] },
  },
  blockExplorers: {
    default: { name: 'Radius Explorer', url: 'https://testnet.radiustech.xyz' },
  },
});

export const RPC_URL = process.env.RADIUS_RPC_URL || 'https://rpc.testnet.radiustech.xyz';
export const FAUCET_BASE = process.env.RADIUS_FAUCET_BASE || 'https://testnet.radiustech.xyz/api/v1/faucet';
export const SBC_ADDRESS = process.env.RADIUS_SBC_ADDRESS || '0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb';
export const SBC_DECIMALS = 6;

export const ERC20_ABI = [
  {
    type: 'function',
    name: 'balanceOf',
    inputs: [{ name: 'account', type: 'address' }],
    outputs: [{ name: '', type: 'uint256' }],
    stateMutability: 'view',
  },
  {
    type: 'function',
    name: 'transfer',
    inputs: [
      { name: 'to', type: 'address' },
      { name: 'amount', type: 'uint256' },
    ],
    outputs: [{ name: '', type: 'bool' }],
    stateMutability: 'nonpayable',
  },
];

export function getPublicClient() {
  return createPublicClient({ chain: radiusTestnet, transport: http(RPC_URL) });
}

export function txExplorerUrl(txHash) {
  return `${radiusTestnet.blockExplorers.default.url}/tx/${txHash}`;
}

export function addressExplorerUrl(address) {
  return `${radiusTestnet.blockExplorers.default.url}/address/${address}`;
}
