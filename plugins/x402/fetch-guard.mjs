// Applied only to the payment CLI subprocess. Every request connects directly
// to a checked public IP while retaining the destination hostname for TLS/SNI.
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import { request as httpsRequest } from 'node:https';
import { createGunzip, createInflate, createBrotliDecompress } from 'node:zlib';

const target = new URL(process.env.HERMES_X402_TARGET);
const rpc = new URL('https://rpc.testnet.radiustech.xyz');
const permitted = new Set([target.origin, rpc.origin]);
const maxBytes = 1_048_576;

function publicAddress(address) {
  if (isIP(address) === 4) {
    const [a, b] = address.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224 || (a === 100 && b >= 64 && b <= 127)
      || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && (b === 0 || b === 168))
      || (a === 198 && (b === 18 || b === 19 || b === 51)) || (a === 203 && b === 0));
  }
  const ip = address.toLowerCase();
  return isIP(address) === 6 && /^[23]/i.test(ip)
    && !(ip.startsWith('2001:') && Number.parseInt(ip.split(':')[1] || '0', 16) < 0x200)
    && !ip.startsWith('2001:db8:') && !ip.startsWith('2002:') && !/^3fff:/i.test(ip);
}

globalThis.fetch = async (input, init = {}) => {
  const request = new Request(input, init);
  const url = new URL(request.url);
  if (!permitted.has(url.origin) || url.username || url.password) throw new Error('x402 subprocess destination refused');
  // Exact seller URL: neither redirects nor a challenge may introduce another path.
  if (url.origin === target.origin && url.href !== target.href) throw new Error('x402 seller URL changed');
  request.signal.throwIfAborted();
  const addresses = await lookup(url.hostname, { all: true });
  if (!addresses.length || addresses.some(({ address }) => !publicAddress(address))) throw new Error('x402 subprocess address refused');
  const pinned = addresses[0];
  const body = request.body ? Buffer.from(await request.arrayBuffer()) : null;
  if (body && body.length > maxBytes) throw new Error('x402 request exceeds 1 MiB');
  const headers = Object.fromEntries(request.headers);
  headers.host = url.host;
  headers['accept-encoding'] = 'identity';
  if (body) headers['content-length'] = String(body.length);
  return new Promise((resolve, reject) => {
    const req = httpsRequest(url, {
      method: request.method, headers, agent: false, rejectUnauthorized: true, servername: url.hostname,
      signal: request.signal, timeout: 30_000,
      // Never perform a second DNS lookup between validation and connection.
      lookup(_hostname, options, callback) {
        if (typeof options === 'function') { callback = options; options = {}; }
        if (options?.all) callback(null, [pinned]);
        else callback(null, pinned.address, pinned.family);
      },
    }, (res) => {
      const encoding = String(res.headers['content-encoding'] || '').toLowerCase();
      const decoder = encoding === 'gzip' ? createGunzip() : encoding === 'deflate' ? createInflate()
        : encoding === 'br' ? createBrotliDecompress() : null;
      if (encoding && encoding !== 'identity' && !decoder) {
        res.destroy(); reject(new Error('unsupported x402 response encoding')); return;
      }
      const stream = decoder ? res.pipe(decoder) : res;
      const chunks = [];
      let size = 0;
      const fail = (error) => { res.destroy(); if (decoder) decoder.destroy(); reject(error); };
      res.on('error', fail);
      stream.on('error', fail);
      stream.on('data', (chunk) => {
        size += chunk.length;
        if (size > maxBytes) { fail(new Error('x402 response exceeds 1 MiB')); return; }
        chunks.push(chunk);
      });
      stream.on('end', () => {
        try {
          const responseHeaders = new Headers();
          for (const [key, value] of Object.entries(res.headers)) {
            if (Array.isArray(value)) value.forEach(v => responseHeaders.append(key, v));
            else if (value !== undefined) responseHeaders.set(key, value);
          }
          const response = new Response([204, 205, 304].includes(res.statusCode) ? null : Buffer.concat(chunks), {
            status: res.statusCode, statusText: res.statusMessage, headers: responseHeaders,
          });
          Object.defineProperty(response, 'url', { value: url.href });
          resolve(response);
        } catch (error) { fail(error); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('x402 request timed out')));
    req.on('error', reject);
    // http.request has no redirect-follow behavior; a redirect is returned as-is.
    req.end(body);
  });
};
