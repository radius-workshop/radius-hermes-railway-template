import { mkdirSync, existsSync, readFileSync, writeFileSync } from 'fs';
import { encodeFunctionData, parseUnits, serializeTransaction } from 'viem';
import { requestFaucetDrip } from '../faucet.mjs';
import { ERC20_ABI, SBC_ADDRESS, SBC_DECIMALS, getPublicClient, radiusTestnet, addressExplorerUrl } from '../radius-chain.mjs';

function readIfExists(path) {
  return existsSync(path) ? readFileSync(path, 'utf8').trim() : '';
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

  async initialize() {
    mkdirSync(this.walletDir, { recursive: true });

    const apiKey = process.env.PARA_API_KEY || '';
    if (!apiKey) {
      throw new Error('PARA_API_KEY is required when para wallet is enabled.');
    }

    try {
      const mod = await import('@getpara/server-sdk');
      const ParaServer = mod.default || mod.ParaServer;
      if (ParaServer) {
        this.para = new ParaServer(apiKey);
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

    const res = await fetch('https://api.getpara.com/v1/wallets/pregenerated', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${process.env.PARA_API_KEY}`,
      },
      body: JSON.stringify({
        identifier: this.identifier,
        identifierType: process.env.PARA_WALLET_IDENTIFIER_TYPE || 'CUSTOM_ID',
        environment: process.env.PARA_ENVIRONMENT || 'beta',
      }),
    });

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`Failed to resolve Para wallet: ${res.status} ${text}`);
    }

    return res.json();
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
      return this.para.signMessage({ walletId: this.walletId, message });
    }

    const res = await fetch(`https://api.getpara.com/v1/wallets/${this.walletId}/sign-message`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${process.env.PARA_API_KEY}`,
      },
      body: JSON.stringify({ message, userShare: this.userShare }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || 'Para sign-message failed.');
    return data.signature;
  }

  async sendTransaction({ to, amountSbc }) {
    const amount = parseUnits(String(amountSbc), SBC_DECIMALS);
    const data = encodeFunctionData({
      abi: ERC20_ABI,
      functionName: 'transfer',
      args: [to, amount],
    });

    const tx = {
      chainId: radiusTestnet.id,
      to: SBC_ADDRESS,
      data,
      value: 0n,
      nonce: await this.publicClient.getTransactionCount({ address: await this.getAddress() }),
      gas: 200000n,
      maxFeePerGas: 1_000_000_000n,
      maxPriorityFeePerGas: 1_000_000_000n,
      type: 'eip1559',
    };

    if (this.para && typeof this.para.signTransaction === 'function') {
      const signed = await this.para.signTransaction({ walletId: this.walletId, transaction: tx });
      return this.publicClient.sendRawTransaction({ serializedTransaction: signed });
    }

    const serialized = serializeTransaction(tx);
    const res = await fetch(`https://api.getpara.com/v1/wallets/${this.walletId}/sign-transaction`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${process.env.PARA_API_KEY}`,
      },
      body: JSON.stringify({ serializedTransaction: serialized, userShare: this.userShare }),
    });
    const signData = await res.json();
    if (!res.ok) throw new Error(signData.message || 'Para sign-transaction failed.');

    return this.publicClient.sendRawTransaction({ serializedTransaction: signData.signedTransaction });
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
