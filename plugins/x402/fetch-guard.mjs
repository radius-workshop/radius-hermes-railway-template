// Applied only to the payment CLI subprocess. Its first request otherwise follows
// redirects before the SDK can apply its payment replay protection.
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

const originalFetch = globalThis.fetch;
const target = new URL(process.env.HERMES_X402_TARGET);
const rpc = new URL('https://rpc.testnet.radiustech.xyz');
const permitted = new Set([target.origin, rpc.origin]);

function publicAddress(address) {
  // Reject special IPv4 ranges and all non-global IPv6 ranges. DNS checks repeat
  // on every fetch; operators must still trust approved DNS against rebinding.
  if (isIP(address) === 4) {
    const [a, b] = address.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224 || (a === 100 && b >= 64 && b <= 127)
      || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 0 || b === 168))
      || (a === 198 && (b === 18 || b === 19 || b === 51)) || (a === 203 && b === 0));
  }
  return isIP(address) === 6 && /^[23]/i.test(address) && !address.toLowerCase().startsWith('2001:db8:');
}

globalThis.fetch = async (input, init = {}) => {
  const url = new URL(input instanceof Request ? input.url : input);
  if (!permitted.has(url.origin) || url.username || url.password) throw new Error('x402 subprocess destination refused');
  // Exact seller URL: neither redirects nor a challenge may introduce another path.
  if (url.origin === target.origin && url.href !== target.href) throw new Error('x402 seller URL changed');
  const addresses = await lookup(url.hostname, { all: true });
  if (!addresses.length || addresses.some(({ address }) => !publicAddress(address))) throw new Error('x402 subprocess address refused');
  return originalFetch(input, { ...init, redirect: 'manual' });
};
