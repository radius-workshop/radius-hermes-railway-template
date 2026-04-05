#!/usr/bin/env node
import { initializeWallets } from './wallet-manager.mjs';

const setDefaultArg = process.argv.find((arg) => arg.startsWith('--set-default='));
const newDefault = setDefaultArg ? setDefaultArg.split('=')[1] : undefined;

const { manager } = await initializeWallets({ autoFund: false });
if (newDefault) {
  manager.setDefaultWallet(newDefault);
  await manager.initializeAll({ autoFund: false });
}

const wallets = await manager.describeWallets();
console.log(JSON.stringify({ defaultWallet: manager.defaultWallet, wallets }, null, 2));
