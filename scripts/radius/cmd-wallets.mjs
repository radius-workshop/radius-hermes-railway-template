#!/usr/bin/env node
import { initializeWallets } from './wallet-manager.mjs';

const { manager } = await initializeWallets();
const wallets = await manager.describeWallets();
console.log(JSON.stringify({ defaultWallet: manager.defaultWallet, wallets }, null, 2));
