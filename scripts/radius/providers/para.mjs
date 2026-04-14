import { mkdirSync, existsSync, readFileSync, writeFileSync } from 'fs';
import { parseUnits } from 'viem';
import { requestFaucetDrip } from '../faucet.mjs';
import { ERC20_ABI, SBC_ADDRESS, SBC_DECIMALS, getPublicClient, radiusTestnet, addressExplorerUrl } from '../radius-chain.mjs';

function readIfExists(path) {
  return existsSync(path) ? readFileSync(path, 'utf8').trim() : '';
}

function toHexSignature(signature) {
  if (!signature) return signature;
  return signature.startsWith('0x') ? signature : `0x${signature}`;
}

export class ParaRadiusWalletProvider {
  constructor({ name = 'para', walletDir }) {
    this.name = name;
    this.walletDir = walletDir;
    this.publicClient = getPublicClient();
    this.providerFile = `${walletDir}/provider`;
    this.addressFile = `${walletDir}/address`;
    this.walletIdFile = `${walletDir}/wallet-id`;
    this.userShareFile = `${walletDir}/user-share`;
    this.identifierFile = `${walletDir}/identifier`;
    this.address = '';
    this.walletId = '';
    this.userShare = '';
    this.identifier = '';
    this.para = null;
  }

  get apiBase() {
    return (process.env.PARA_ENVIRONMENT || 'beta') === 'beta'
      ? 'https://api.beta.getpara.com'
      : 'https://api.getpara.com';
  }

  get apiHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-API-Key': process.env.PARA_API_KEY,
    };
  }

  async _api(path, { method = 'GET', body } = {}) {
    const res = await fetch(`${this.apiBase}${path}`, {
      method,
      headers: this.apiHeaders,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.message || data.code || `Para API error (${res.status})`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  async initialize() {
    mkdirSync(this.walletDir, { recursive: true });

    const apiKey = process.env.PARA_API_KEY || '';
    if (!apiKey) {
      throw new Error('PARA_API_KEY is required when para wallet is enabled.');
    }

    try {
      const mod = await import('@getpara/server-sdk');
      const ParaServer = mod.default || mod.ParaServer || mod.Para;
      if (ParaServer) {
        this.para = new ParaServer(apiKey, { disableWebSockets: true });
      }
    } catch {
      console.warn('[radius:para] @getpara/server-sdk not installed, using REST-only mode.');
    }

    this.userShare = process.env.PARA_USER_SHARE || readIfExists(this.userShareFile);
    if (this.userShare && this.para && typeof this.para.setUserShare === 'function') {
      await this.para.setUserShare(this.userShare);
    }

    const defaultIdentifier = `agent:${process.env.RAILWAY_ENVIRONMENT || 'local'}:${(process.env.AGENT_NAME || 'hermes').toLowerCase().replace(/[^a-z0-9-]/g, '-')}`;
    this.identifier = process.env.PARA_WALLET_IDENTIFIER || readIfExists(this.identifierFile) || defaultIdentifier;

    const wallet = await this._resolveOrCreateWallet();
    this.walletId = wallet.id || wallet.walletId || wallet.wallet_id || readIfExists(this.walletIdFile);
    this.address = wallet.address || wallet.walletAddress || wallet.evmAddress || readIfExists(this.addressFile);

    const walletUserShare = wallet.userShare || wallet.user_share;
    if (walletUserShare) {
      this.userShare = walletUserShare;
      if (this.para && typeof this.para.setUserShare === 'function') {
        await this.para.setUserShare(this.userShare);
      }
    }

    writeFileSync(this.providerFile, 'para');
    if (this.address) writeFileSync(this.addressFile, this.address);
    if (this.walletId) writeFileSync(this.walletIdFile, this.walletId);
    if (this.userShare) writeFileSync(this.userShareFile, this.userShare, { mode: 0o600 });
    if (this.identifier) writeFileSync(this.identifierFile, this.identifier);
  }

  async _resolveOrCreateWallet() {
    if (this.para && typeof this.para.getOrCreateWallet === 'function') {
      return this.para.getOrCreateWallet({
        identifier: this.identifier,
        identifierType: process.env.PARA_WALLET_IDENTIFIER_TYPE || 'CUSTOM_ID',
        environment: process.env.PARA_ENVIRONMENT || 'beta',
        autoCreate: `${process.env.PARA_AUTO_CREATE || 'true'}`.toLowerCase() !== 'false',
      });
    }

    const idType = process.env.PARA_WALLET_IDENTIFIER_TYPE || 'CUSTOM_ID';
    try {
      const list = await this._api(`/v1/wallets?userIdentifier=${encodeURIComponent(this.identifier)}&userIdentifierType=${encodeURIComponent(idType)}`);
      if (Array.isArray(list.data) && list.data.length) {
        return list.data.find((w) => w.type === 'EVM') || list.data[0];
      }
    } catch {
      // continue to create
    }

    try {
      return await this._api('/v1/wallets', {
        method: 'POST',
        body: {
          type: 'EVM',
          userIdentifier: this.identifier,
          userIdentifierType: idType,
        },
      });
    } catch (error) {
      if (error.status === 409 && error.data?.walletId) {
        return this._api(`/v1/wallets/${error.data.walletId}`);
      }
      throw error;
    }
  }

  async getAddress() {
    if (!this.address) throw new Error('Para wallet address is not ready yet.');
    return this.address;
  }

  async getExplorerUrl() {
    return addressExplorerUrl(await this.getAddress());
  }

  getPublicClient() {
    return this.publicClient;
  }

  async signMessage(message) {
    if (this.para && typeof this.para.signMessage === 'function') {
      return toHexSignature(await this.para.signMessage({ walletId: this.walletId, message }));
    }

    const data = await this._api(`/v1/wallets/${this.walletId}/sign-message`, {
      method: 'POST',
      body: { message },
    });
    return toHexSignature(data.signature);
  }

  async sendTransaction({ to, amountSbc, asset = 'SBC' }) {
    const assetUpper = String(asset || 'SBC').toUpperCase();

    if (this.para && typeof this.para.transfer === 'function') {
      const value = assetUpper === 'SBC' ? parseUnits(String(amountSbc), SBC_DECIMALS).toString() : parseUnits(String(amountSbc), 18).toString();
      const res = await this.para.transfer({ walletId: this.walletId, to, value, tokenAddress: assetUpper === 'SBC' ? SBC_ADDRESS : undefined, chainId: radiusTestnet.id });
      return res.txHash || res.hash || res.transactionHash;
    }

    const value = assetUpper === 'SBC' ? parseUnits(String(amountSbc), SBC_DECIMALS).toString() : parseUnits(String(amountSbc), 18).toString();
    const data = await this._api(`/v1/wallets/${this.walletId}/transfer`, {
      method: 'POST',
      body: {
        to,
        value,
        chainId: radiusTestnet.id,
        ...(assetUpper === 'SBC' ? { tokenAddress: SBC_ADDRESS } : {}),
      },
    });

    return data.txHash || data.transactionHash || data.hash || data.signedTransaction;
  }

  async getSbcBalance() {
    return this.publicClient.readContract({
      address: SBC_ADDRESS,
      abi: ERC20_ABI,
      functionName: 'balanceOf',
      args: [await this.getAddress()],
    });
  }

  async fundFromFaucet() {
    return requestFaucetDrip({
      signer: {
        getAddress: () => this.getAddress(),
        signMessage: (message) => this.signMessage(message),
      },
    });
  }

  async isAvailable() {
    return !!(this.para || process.env.PARA_API_KEY);
  }

  async describe() {
    return {
      name: this.name,
      provider: 'para',
      address: this.address || undefined,
      ready: !!this.address,
      canFund: !!this.address,
      source: this.para ? 'para-server-sdk' : 'para-rest-api',
    };
  }
}
