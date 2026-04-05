#!/usr/bin/env node
import { initializeWallets } from './wallet-manager.mjs';

try {
  const { manifest } = await initializeWallets();
  console.log('[radius] Wallet initialization complete.');
  console.log(JSON.stringify(manifest, null, 2));
} catch (err) {
  console.error(`[radius] Wallet initialization failed: ${err.message}`);
  process.exit(1);
}
