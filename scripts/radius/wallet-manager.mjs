import { mkdirSync, writeFileSync, existsSync, readFileSync } from 'fs';
import { createProvider, SUPPORTED_WALLETS } from './providers/index.mjs';

export function parseWalletList(raw) {
  const fallback = ['local'];
  const list = (raw ?? process.env.RADIUS_WALLETS ?? fallback.join(','))
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

function walletInitShouldAutoFund() {
  const autoFundEnabled = `${process.env.RADIUS_AUTO_FUND_ON_BOOT ?? process.env.RADIUS_AUTO_FUND ?? 'true'}`.toLowerCase();
  return autoFundEnabled !== 'false' && autoFundEnabled !== '0';
}

export class RadiusWalletManager {
  constructor({ hermesHome = process.env.HERMES_HOME || '/data/.hermes' } = {}) {
    this.hermesHome = hermesHome;
    this.rootDir = `${hermesHome}/.radius`;
    this.walletsDir = `${this.rootDir}/wallets`;
    this.manifestPath = `${this.walletsDir}/manifest.json`;
    this.defaultWalletFile = `${this.walletsDir}/default-wallet`;
    this.providers = new Map();
    this.failedProviders = new Map();
    this.walletOrder = parseWalletList();
    this.defaultWallet = process.env.RADIUS_DEFAULT_WALLET || (existsSync(this.defaultWalletFile) ? readFileSync(this.defaultWalletFile, 'utf8').trim() : '') || this.walletOrder[0] || 'local';
  }

  async initializeAll({ autoFund = walletInitShouldAutoFund() } = {}) {
    mkdirSync(this.walletsDir, { recursive: true });

    for (const walletName of this.walletOrder) {
      try {
        const provider = createProvider(walletName, { walletDir: `${this.walletsDir}/${walletName}` });
        await provider.initialize();
        this.providers.set(walletName, provider);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        this.failedProviders.set(walletName, message);
        console.warn(`[radius] Wallet '${walletName}' failed to initialize: ${message}`);
      }
    }

    if (autoFund) {
      const fundSet = parseAutoFundWallets(this.walletOrder);
      for (const walletName of this.walletOrder) {
        const provider = this.providers.get(walletName);
        if (!provider || !fundSet.has(walletName)) continue;
        const result = await provider.fundFromFaucet();
        if (result.ok) {
          console.log(`[radius] Funded ${walletName} wallet${result.txHash ? ` (${result.txHash})` : ''}`);
        } else {
          console.warn(`[radius] Faucet funding failed for ${walletName}: ${result.message || 'unknown error'}`);
        }
      }
    }

    const manifestWallets = [];
    for (const walletName of this.walletOrder) {
      const provider = this.providers.get(walletName);
      if (provider) {
        manifestWallets.push(await provider.describe());
      } else {
        manifestWallets.push({
          name: walletName,
          provider: walletName,
          ready: false,
          canFund: false,
          source: 'unavailable',
          error: this.failedProviders.get(walletName),
        });
      }
    }

    writeFileSync(this.defaultWalletFile, this.defaultWallet);

    const manifest = {
      defaultWallet: this.defaultWallet,
      wallets: manifestWallets,
    };

    writeFileSync(this.manifestPath, JSON.stringify(manifest, null, 2));

    const explicitDefaultProvider = this.providers.get(this.defaultWallet);
    const defaultProvider = explicitDefaultProvider || this.providers.values().next().value;
    if (!explicitDefaultProvider && defaultProvider) {
      console.warn(`[radius] Default wallet '${this.defaultWallet}' is unavailable; using '${defaultProvider.name}' as runtime fallback.`);
    }
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
      const failure = this.failedProviders.get(name);
      if (failure) {
        throw new Error(`Wallet '${name}' is configured but unavailable: ${failure}`);
      }
      throw new Error(`Wallet '${name}' is not configured. Configured wallets: ${this.walletOrder.join(', ')}`);
    }
    return provider;
  }


  setDefaultWallet(walletName) {
    if (!this.walletOrder.includes(walletName)) {
      throw new Error(`Cannot set default wallet to '${walletName}'. Configured wallets: ${this.walletOrder.join(', ')}`);
    }
    this.defaultWallet = walletName;
    writeFileSync(this.defaultWalletFile, walletName);
  }

  async describeWallets() {
    const wallets = [];
    for (const walletName of this.walletOrder) {
      const provider = this.providers.get(walletName);
      if (provider) {
        wallets.push(await provider.describe());
      } else {
        wallets.push({
          name: walletName,
          provider: walletName,
          ready: false,
          canFund: false,
          source: 'unavailable',
          error: this.failedProviders.get(walletName),
        });
      }
    }
    return wallets;
  }
}

export async function initializeWallets(options = {}) {
  const manager = new RadiusWalletManager();
  const manifest = await manager.initializeAll(options);
  return { manager, manifest };
}
