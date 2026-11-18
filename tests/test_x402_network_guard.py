"""Exercise the real Node guard with mocked DNS/HTTPS for each payment rail."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

NODE = shutil.which("node")
GUARD = (Path(__file__).resolve().parents[1] / "plugins/x402/fetch-guard.mjs").as_uri()
NETWORKS = {
    "eip155:72344": "https://rpc.testnet.radiustech.xyz",
    "eip155:723487": "https://rpc.radiustech.xyz",
    "eip155:84532": "https://sepolia.base.org",
    "eip155:8453": "https://mainnet.base.org",
}

SCRIPT = r"""
const assert = require('node:assert/strict');
const dns = require('node:dns/promises');
const https = require('node:https');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { syncBuiltinESMExports } = require('node:module');
const network = process.argv[2];
const networks = JSON.parse(process.argv[3]);
const seller = 'https://seller.example/paid';
const expected = {scheme:'exact', network, asset:'0x'+'a'.repeat(40),
  amount:'100', payTo:'0x'+'1'.repeat(40), maxTimeoutSeconds:60};
let dnsCalls = 0;
let requests = 0;
let changed = false;
dns.lookup = async () => { dnsCalls++; return [{address:'8.8.8.8', family:4}]; };
https.request = (url, options, callback) => {
  requests++;
  assert.equal(options.rejectUnauthorized, true);
  assert.equal(options.servername, url.hostname);
  options.lookup(url.hostname, {}, (error, ip) => {
    assert.equal(error, null); assert.equal(ip, '8.8.8.8');
  });
  const req = new EventEmitter();
  req.end = () => {
    const isSeller = url.href === seller;
    const offer = changed ? {...expected, network:'eip155:1'} : expected;
    const body = isSeller ? JSON.stringify({x402Version:2, accepts:[offer]}) : '{}';
    const res = new PassThrough();
    Object.assign(res, {statusCode:isSeller ? 402 : 200, statusMessage:'OK', headers:{}});
    callback(res); res.end(body);
  };
  return req;
};
syncBuiltinESMExports();
process.env.HERMES_X402_TARGET = seller;
process.env.HERMES_X402_EXPECTED_OFFER = JSON.stringify(expected);
// Ambient configuration must not add an RPC to the guard.
process.env.RADIUS_BASE_RPC_URL = 'https://evil.example';
(async () => {
  if (!Object.hasOwn(networks, network)) {
    await assert.rejects(import(process.argv[1]), /network is unsupported/);
    assert.equal(dnsCalls, 0); assert.equal(requests, 0);
    return;
  }
  await import(process.argv[1]);
  assert.equal((await fetch(networks[network])).status, 200);
  assert.equal((await fetch(seller)).status, 402);
  assert.equal(dnsCalls, 2); assert.equal(requests, 2);
  for (const url of [...Object.values(networks).filter(url => url !== networks[network]),
                     'https://evil.example', 'http://mainnet.base.org',
                     'https://seller.example/other']) {
    await assert.rejects(fetch(url), /destination refused|seller URL changed/);
    assert.equal(dnsCalls, 2); assert.equal(requests, 2);
  }
  changed = true;
  await assert.rejects(fetch(seller), /offer changed after inspection/);
  assert.equal(requests, 3);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "Node runtime required for CLI fetch guard tests")
class NetworkGuardTests(unittest.TestCase):
    def run_guard(self, network):
        proc = subprocess.run(
            [NODE, "-e", SCRIPT, GUARD, network, json.dumps(NETWORKS)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_selected_rpc_only_and_exact_offer_binding_on_each_network(self):
        for network in NETWORKS:
            with self.subTest(network=network):
                self.run_guard(network)

    def test_unknown_network_fails_before_network_access(self):
        for network in ("eip155:1", "base", "toString", "__proto__", ""):
            with self.subTest(network=network):
                self.run_guard(network)


if __name__ == "__main__":
    unittest.main()
