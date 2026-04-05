import { LocalRadiusWalletProvider } from './local.mjs';
import { ParaRadiusWalletProvider } from './para.mjs';

export const SUPPORTED_WALLETS = ['local', 'para'];

export function createProvider(name, { walletDir }) {
  if (name === 'local') return new LocalRadiusWalletProvider({ name, walletDir });
  if (name === 'para') return new ParaRadiusWalletProvider({ name, walletDir });
  throw new Error(`Unsupported wallet provider: ${name}`);
}
