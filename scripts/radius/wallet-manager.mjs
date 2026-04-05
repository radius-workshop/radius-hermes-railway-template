import { mkdirSync, writeFileSync, existsSync, readFileSync } from 'fs';
import { createProvider, SUPPORTED_WALLETS } from './providers/index.mjs';

export function parseWalletList(raw) {
  const fallback = process.env.RADIUS_PRIVATE_KEY ? ['local'] : ['local'];
  const list = (raw || process.env.RADIUS_WALLETS || fallback.join(','))
    .split(',')
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);

  const deduped = [...new Set(list)].filter((x) => SUPPORTED_WALLETS.includes(x));
  return deduped.length ? deduped : ['local'];
}

export function parseAutoFundWallets(configuredWallets) {
  const list = (process.env.RADIUS_AUTO_FUND_WALLETS || configuredWallets.join(','))
    .split(',')
    .map((x) => x.trim().toLowerCase())
    .filter(Boolean);

  return new Set(list);
}

export class RadiusWalletManager {
  constructor({ hermesHome = process.env.HERMES_HOME || '/data/.hermes' } = {}) {
    this.hermesHome = hermesHome;
    this.rootDir = `${hermesHome}/.radius`;
    this.walletsDir = `${this.rootDir}/wallets`;
    this.manifestPath = `${this.walletsDir}/manifest.json`;
    this.providers = new Map();
    this.walletOrder = parseWalletList();
    this.defaultWallet = process.env.RADIUS_DEFAULT_WALLET || this.walletOrder[0] || 'local';
  }

  async initializeAll() {
    mkdirSync(this.walletsDir, { recursive: true });

    for (const walletName of this.walletOrder) {
      const provider = createProvider(walletName, { walletDir: `${this.walletsDir}/${walletName}` });
      await provider.initialize();
      this.providers.set(walletName, provider);
    }

    const autoFundEnabled = `${process.env.RADIUS_AUTO_FUND_ON_BOOT ?? process.env.RADIUS_AUTO_FUND ?? 'true'}`.toLowerCase();
    if (autoFundEnabled !== 'false' && autoFundEnabled !== '0') {
      const fundSet = parseAutoFundWallets(this.walletOrder);
      for (const walletName of this.walletOrder) {
        if (!fundSet.has(walletName)) continue;
        const result = await this.providers.get(walletName).fundFromFaucet();
        if (result.ok) {
          console.log(`[radius] Funded ${walletName} wallet${result.txHash ? ` (${result.txHash})` : ''}`);
        } else {
          console.warn(`[radius] Faucet funding failed for ${walletName}: ${result.message || 'unknown error'}`);
        }
      }
    }

    const manifest = {
      defaultWallet: this.defaultWallet,
      wallets: await Promise.all(this.walletOrder.map(async (name) => this.providers.get(name).describe())),
    };

    writeFileSync(this.manifestPath, JSON.stringify(manifest, null, 2));

    // legacy compatibility with single-wallet env/file readers
    const defaultProvider = this.providers.get(this.defaultWallet) || this.providers.values().next().value;
    if (defaultProvider) {
      const address = await defaultProvider.getAddress();
      writeFileSync(`${this.rootDir}/address`, address);
      process.env.RADIUS_WALLET_ADDRESS = address;
      if (defaultProvider.name === 'local' && existsSync(`${this.walletsDir}/local/key`)) {
        const key = readFileSync(`${this.walletsDir}/local/key`, 'utf8').trim();
        writeFileSync(`${this.rootDir}/key`, key, { mode: 0o600 });
        process.env.RADIUS_PRIVATE_KEY = key;
      }
    }

    process.env.RADIUS_WALLET_MANIFEST = this.manifestPath;
    return manifest;
  }

  getProvider(walletName) {
    const name = walletName || process.env.RADIUS_DEFAULT_WALLET || this.defaultWallet;
    const provider = this.providers.get(name);
    if (!provider) {
      throw new Error(`Wallet '${name}' is not configured. Configured wallets: ${[...this.providers.keys()].join(', ')}`);
    }
    return provider;
  }

  async describeWallets() {
    return Promise.all(this.walletOrder.map((name) => this.providers.get(name).describe()));
  }
}

export async function initializeWallets() {
  const manager = new RadiusWalletManager();
  const manifest = await manager.initializeAll();
  return { manager, manifest };
}
