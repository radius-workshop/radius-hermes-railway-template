import { mkdirSync, readFileSync, writeFileSync, existsSync, chmodSync } from 'fs';
import { createWalletClient, formatUnits, http, parseUnits } from 'viem';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';
import { requestFaucetDrip } from '../faucet.mjs';
import { ERC20_ABI, SBC_ADDRESS, SBC_DECIMALS, getPublicClient, radiusTestnet, RPC_URL, addressExplorerUrl } from '../radius-chain.mjs';

export class LocalRadiusWalletProvider {
  constructor({ name = 'local', walletDir }) {
    this.name = name;
    this.walletDir = walletDir;
    this.keyFile = `${walletDir}/key`;
    this.addressFile = `${walletDir}/address`;
    this.providerFile = `${walletDir}/provider`;
    this.privateKey = null;
    this.account = null;
    this.walletClient = null;
    this.publicClient = getPublicClient();
  }

  async initialize() {
    mkdirSync(this.walletDir, { recursive: true });

    const legacyKey = `${process.env.HERMES_HOME || '/data/.hermes'}/.radius/key`;
    let privateKey = process.env.RADIUS_LOCAL_PRIVATE_KEY || process.env.RADIUS_PRIVATE_KEY || '';

    if (!privateKey && existsSync(this.keyFile)) {
      privateKey = readFileSync(this.keyFile, 'utf8').trim();
    }
    if (!privateKey && existsSync(legacyKey)) {
      privateKey = readFileSync(legacyKey, 'utf8').trim();
    }

    if (!privateKey) {
      const autoGenerate = `${process.env.RADIUS_LOCAL_AUTO_GENERATE || 'true'}`.toLowerCase();
      if (autoGenerate === 'false' || autoGenerate === '0') {
        throw new Error('Local wallet is enabled but no private key is configured and auto-generation is disabled.');
      }
      privateKey = generatePrivateKey();
      console.log('[radius:local] Generated new local wallet key.');
    }

    this.privateKey = privateKey;
    this.account = privateKeyToAccount(privateKey);
    this.walletClient = createWalletClient({
      account: this.account,
      chain: radiusTestnet,
      transport: http(RPC_URL),
    });

    writeFileSync(this.keyFile, privateKey, { mode: 0o600 });
    chmodSync(this.keyFile, 0o600);
    writeFileSync(this.addressFile, this.account.address);
    writeFileSync(this.providerFile, 'local');
  }

  async getAddress() {
    if (!this.account) throw new Error('Local wallet not initialized.');
    return this.account.address;
  }

  async getExplorerUrl() {
    return addressExplorerUrl(await this.getAddress());
  }

  getPublicClient() {
    return this.publicClient;
  }

  async signMessage(message) {
    return this.walletClient.signMessage({ message });
  }

  async sendTransaction({ to, amountSbc }) {
    const amount = parseUnits(String(amountSbc), SBC_DECIMALS);
    return this.walletClient.writeContract({
      address: SBC_ADDRESS,
      abi: ERC20_ABI,
      functionName: 'transfer',
      args: [to, amount],
    });
  }

  async getSbcBalance() {
    return this.publicClient.readContract({
      address: SBC_ADDRESS,
      abi: ERC20_ABI,
      functionName: 'balanceOf',
      args: [await this.getAddress()],
    });
  }

  async getFormattedBalance() {
    return formatUnits(await this.getSbcBalance(), SBC_DECIMALS);
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
    return !!this.account;
  }

  async describe() {
    const ready = await this.isAvailable();
    return {
      name: this.name,
      provider: 'local',
      address: ready ? await this.getAddress() : undefined,
      ready,
      canFund: ready,
      source: 'local-private-key',
    };
  }
}
