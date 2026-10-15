"""Offline payment-boundary tests: no wallet keys, RPC calls or fund transfers."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('template_x402', ROOT / 'plugins/x402/__init__.py', submodule_search_locations=[str(ROOT / 'plugins/x402')])
x = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = x
spec.loader.exec_module(x)
from template_x402.benchmark import x402_benchmark


def offer(**changes):
    return {"scheme": "exact", "network": x.NETWORK, "asset": x.ASSET, "amount": "100",
            "payTo": "0x" + "1" * 40, "maxTimeoutSeconds": 60, **changes}


def response(status=402, offers=None):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps({"x402Version": 2, "accepts": offers or [offer()]}).encode()
    return r


class PaymentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_HOME": self.tmp.name, "X402_ALLOWED_HOSTS": "seller.example", "X402_MAX_PER_CALL": "0.01"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.dns = patch.object(socket, 'getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.params = {"url": "https://seller.example/paid?token=secret"}

    def test_caps_fail_closed_before_request(self):
        for value in ['NaN', 'Infinity', '-1', '0', 'bad', '0.0000001', '1e100']:
            with self.subTest(value=value), patch.dict(os.environ, {'X402_MAX_PER_CALL': value}), patch.object(x, '_send') as send:
                self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
                send.assert_not_called()
        for value in ['NaN', '-1', '0', 'Infinity']:
            with self.subTest(value=value), patch.object(x, '_send') as send:
                self.assertEqual(x.x402_request({**self.params, 'max_amount': value})['outcome'], 'error')
                send.assert_not_called()

    def test_caller_cannot_raise_cap(self):
        with patch.object(x, '_send', return_value=response(offers=[offer(amount='20000')])), patch.object(x, '_pay_radius_cli') as pay:
            self.assertEqual(x.x402_request({**self.params, 'max_amount': '1'})['outcome'], 'refused')
            pay.assert_not_called()

    def test_mainnet_base_and_malformed_challenges_never_sign(self):
        bad = [offer(network='eip155:723487'), offer(network='eip155:84532'), offer(asset='0x'+'2'*40),
               offer(amount='-1'), offer(amount='0'), offer(amount='1.5'), offer(amount=100), offer(amount=str(2**256)),
               offer(payTo='0x'+'0'*40), offer(payTo='bad'), offer(scheme='upto'), offer(maxTimeoutSeconds=301),
               offer(maxTimeoutSeconds=True), offer(extra=[])]
        for req in bad:
            with self.subTest(req=req), patch.object(x, '_send', return_value=response(offers=[req])), patch.object(x, '_pay_radius_cli') as pay:
                self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
                pay.assert_not_called()
        with patch.dict(os.environ, {'X402_ALLOW_MAINNET': 'true'}), patch.object(x, '_send', return_value=response(offers=[offer(network='eip155:723487')])), patch.object(x, '_pay_radius_cli') as pay:
            self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
            pay.assert_not_called()

    def test_unsafe_destinations_fail_before_http(self):
        urls = ['http://seller.example/x', 'https://seller.example:8443/x', 'https://evil.example/x',
                'https://seller.example.evil.example/x', 'https://user:pass@seller.example/x',
                'https://seller.example/x#fragment', 'https://127.0.0.1/x']
        for url in urls:
            with self.subTest(url=url), patch.object(x, '_send') as send:
                self.assertEqual(x.x402_request({'url': url})['outcome'], 'error')
                send.assert_not_called()
        for address in ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '::ffff:127.0.0.1']:
            with self.subTest(address=address), patch.object(socket, 'getaddrinfo', return_value=[(2, 1, 6, '', (address, 443))]), patch.object(x, '_send') as send:
                self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
                send.assert_not_called()
        with patch.dict(os.environ, {'X402_ALLOWED_HOSTS': ''}), patch.object(x, '_send') as send:
            self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
            send.assert_not_called()

    def test_python_transport_pins_checked_address_and_retains_tls_hostname(self):
        public = lambda ip: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
        wire = Mock()
        wire.makefile.return_value = io.BytesIO(b"HTTP/1.1 302 Found\r\nContent-Length: 8\r\nLocation: https://evil.example/\r\n\r\nredirect")
        context = Mock()
        context.wrap_socket.return_value = wire
        with patch.object(socket, 'getaddrinfo', side_effect=[public('8.8.8.8'), public('8.8.4.4'), public('127.0.0.1')]) as dns, \
                patch.object(socket, 'create_connection', return_value=wire) as connect, \
                patch.object(x.ssl, 'create_default_context', return_value=context):
            result = x._send('GET', self.params['url'], {}, None)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.text, 'redirect')
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(connect.call_args.args[0], ('8.8.4.4', 443))
        context.wrap_socket.assert_called_once_with(wire, server_hostname='seller.example')

    def test_dry_run_and_bad_body_or_headers_do_not_sign(self):
        for extra in [{'dry_run': 'true'}, {'body': '@/data/.hermes/.env'}, {'body': '-'}, {'headers': {'Host': 'localhost'}}, {'headers': {'x': 'a\r\nb'}}]:
            with self.subTest(extra=extra), patch.object(x, '_send') as send:
                self.assertEqual(x.x402_request({**self.params, **extra})['outcome'], 'error')
                send.assert_not_called()
        with patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli') as pay:
            self.assertEqual(x.x402_request({**self.params, 'dry_run': True})['outcome'], 'quoted')
            pay.assert_not_called()

    def test_http_200_without_payment_report_is_unknown(self):
        with patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli', return_value={'status': 200, 'payment': None, 'body': 'secret content'}):
            result = x.x402_request(self.params)
        self.assertEqual(result['outcome'], 'payment_unknown')
        self.assertEqual(result['delivery'], 'delivered')
        ledger = x._ledger_path().read_text()
        self.assertNotIn('secret content', ledger)
        self.assertNotIn('token=secret', ledger)

    def test_report_is_unverified_and_matches_quote(self):
        payment = {'paid': True, 'network': x.NETWORK, 'protocol': 'x402', 'scheme': 'exact', 'asset': x.ASSET,
                   'amountWei': '100', 'payTo': offer()['payTo'], 'txHash': '0x'+'a'*64}
        with patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli', return_value={'status': 200, 'payment': payment, 'cli_exit_code': 0}):
            result = x.x402_request(self.params)
        self.assertEqual(result['outcome'], 'settlement_reported')
        self.assertEqual(result['reconciliation'], 'not_verified')
        with patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli', return_value={'status': 200, 'payment': {**payment, 'amountWei': '200'}}):
            self.assertEqual(x.x402_request(self.params)['outcome'], 'payment_unknown')

    def test_cli_paid_requires_successful_exit_and_no_contradictory_settlement(self):
        payment = {'paid': True, 'network': x.NETWORK, 'protocol': 'x402', 'scheme': 'exact', 'asset': x.ASSET,
                   'amountWei': '100', 'payTo': offer()['payTo'], 'txHash': '0x'+'a'*64}
        bad_receipt = x.base64.b64encode(json.dumps({'success': False}).encode()).decode()
        for changes in ({'cli_exit_code': 1}, {'payment': {**payment, 'paid': False}},
                        {'headers': {'payment-response': bad_receipt}}):
            result = {'status': 200, 'payment': payment, 'cli_exit_code': 0, **changes}
            with self.subTest(changes=changes), patch.object(x, '_send', return_value=response()), \
                    patch.object(x, '_pay_radius_cli', return_value=result):
                entry = x.x402_request(self.params)
            self.assertEqual(entry['outcome'], 'payment_unknown')
            self.assertEqual(entry['delivery'], 'delivered')
            self.assertEqual(entry['reconciliation'], 'not_verified')

    def test_timeout_after_signing_is_unknown_not_safe_retry(self):
        with patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli', side_effect=TimeoutError):
            self.assertEqual(x.x402_request(self.params)['outcome'], 'payment_unknown')
        self.assertEqual(x.x402_ledger({})['entries'][0]['outcome'], 'payment_unknown')

    def test_ledger_failures_block_signing_and_final_error_preserves_result(self):
        with patch.object(x, '_append_ledger', side_effect=OSError), patch.object(x, '_send') as send:
            self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
            send.assert_not_called()
        with patch.object(x, '_append_ledger', side_effect=[None, OSError, None]), patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli') as pay:
            self.assertEqual(x.x402_request(self.params)['outcome'], 'error')
            pay.assert_not_called()
        with patch.object(x, '_append_ledger', side_effect=[None, None, OSError]), patch.object(x, '_send', return_value=response()), patch.object(x, '_pay_radius_cli', return_value={'status': 200}):
            result = x.x402_request(self.params)
        self.assertEqual(result['outcome'], 'payment_unknown')
        self.assertIn('ledger_error', result)

    def test_ledger_skips_bad_rows_and_collapses_intent_events(self):
        x._append_ledger({'id': 'one', 'outcome': 'pending'})
        x._append_ledger({'id': 'one', 'outcome': 'quoted'})
        with x._ledger_path().open('a') as f:
            f.write('bad json\n[]\n')
        result = x.x402_ledger({})
        self.assertEqual(result['entries_total'], 1)
        self.assertEqual(result['malformed_lines'], 2)
        self.assertEqual(result['entries'][0]['outcome'], 'quoted')

    def test_cli_040_arguments_are_bounded_without_approval_or_file_reads(self):
        with patch.object(x, '_run_radius_cli', return_value={}) as run:
            x._pay_radius_cli('GET', self.params['url'], {}, None, x.Decimal('0.001'), offer())
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('wallet')+1], 'pay')
        self.assertEqual(args[args.index('--protocol')+1], 'x402')
        self.assertEqual(args[args.index('--networks')+1], 'radius')
        self.assertEqual(args[args.index('--threshold')+1], '0.001')
        self.assertNotIn('--approve-permit2', args)
        self.assertNotIn('-y', args)
        self.assertEqual(run.call_args.kwargs['expected_offer'], offer())

    @unittest.skipUnless(NODE, 'Node runtime required for CLI fetch guard test')
    def test_node_guard_blocks_redirects_changed_urls_and_private_dns(self):
        guard = (ROOT / 'plugins/x402/fetch-guard.mjs').as_uri()
        script = r"""
const assert = require('node:assert/strict');
const dns = require('node:dns/promises');
const https = require('node:https');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { syncBuiltinESMExports } = require('node:module');
let address = '8.8.8.8';
dns.lookup = async () => {
  const result = [{ address, family: 4 }];
  address = '127.0.0.1'; // DNS would rebind if transport performed another lookup.
  return result;
};
const calls = [];
https.request = (url, options, callback) => {
  calls.push({url, options});
  assert.equal(options.rejectUnauthorized, true);
  assert.equal(options.agent, false);
  assert.equal(options.servername, 'seller.example');
  options.lookup(url.hostname, {}, (error, ip, family) => {
    assert.equal(error, null); assert.equal(ip, '8.8.8.8'); assert.equal(family, 4);
  });
  const req = new EventEmitter();
  req.end = () => {
    const res = new PassThrough();
    Object.assign(res, { statusCode: 302, statusMessage: 'Found', headers: { location: 'https://evil.example/' } });
    callback(res); res.end('redirect');
  };
  return req;
};
syncBuiltinESMExports();
globalThis.fetch = () => { throw new Error('native fetch must never resolve the hostname again'); };
process.env.HERMES_X402_TARGET = 'https://seller.example/paid';
process.env.HERMES_X402_EXPECTED_OFFER = JSON.stringify({scheme:'exact', network:'eip155:72344'});
(async () => {
  await import(process.argv[1]);
  const response = await fetch('https://seller.example/paid');
  assert.equal(response.status, 302);
  assert.equal(await response.text(), 'redirect');
  assert.equal(address, '127.0.0.1');
  await assert.rejects(fetch('https://evil.example/paid'));
  await assert.rejects(fetch('https://seller.example/changed'));
  await assert.rejects(fetch('https://seller.example/paid'));
  assert.equal(calls.length, 1);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        proc = subprocess.run([NODE, '-e', script, guard], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_inspected_offer_is_passed_to_guard_before_cli_launch(self):
        result = Mock(stdout='{"status":200}', returncode=0)
        with patch.dict(os.environ, {'RADIUS_CLI_BIN': '/installed/radius-cli'}), \
                patch.object(x.subprocess, 'run', return_value=result) as run:
            x._run_radius_cli(['--json'], target_url=self.params['url'], expected_offer=offer())
        self.assertEqual(json.loads(run.call_args.kwargs['env']['HERMES_X402_EXPECTED_OFFER']), offer())
        self.assertIn('fetch-guard.mjs', run.call_args.kwargs['env']['NODE_OPTIONS'])
        with patch.dict(os.environ, {'RADIUS_CLI_BIN': '/installed/radius-cli'}), \
                patch.object(x.subprocess, 'run') as run:
            with self.assertRaises(x.X402Error):
                x._run_radius_cli(['--json'], target_url=self.params['url'])
            run.assert_not_called()

    @unittest.skipUnless(NODE, 'Node runtime required for challenge binding test')
    def test_repeated_quote_cannot_swap_recipient_before_signing(self):
        guard = (ROOT / 'plugins/x402/fetch-guard.mjs').as_uri()
        script = r"""
const assert = require('node:assert/strict');
const dns = require('node:dns/promises');
const https = require('node:https');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { syncBuiltinESMExports } = require('node:module');
dns.lookup = async () => [{address:'8.8.8.8', family:4}];
const expected = {scheme:'exact', network:'eip155:72344', asset:'0x'+'a'.repeat(40),
  amount:'100', payTo:'0x'+'1'.repeat(40), maxTimeoutSeconds:60, extra:{assetTransferMethod:'permit2'}};
const swapped = {...expected, payTo:'0x'+'2'.repeat(40)};
let offers = [swapped];
let headerChallenge = false;
let calls = 0;
let signed = 0;
https.request = (_url, _options, callback) => {
  calls++;
  const req = new EventEmitter();
  req.end = () => {
    const challenge = {x402Version:2, accepts:offers, resource:{url:'https://seller.example/paid'}, extensions:{unchanged:true}};
    const body = JSON.stringify(challenge);
    const res = new PassThrough();
    Object.assign(res, {statusCode:402, statusMessage:'Payment Required', headers:headerChallenge
      ? {'payment-required':Buffer.from(body).toString('base64')} : {}});
    callback(res); res.end(body);
  };
  return req;
};
syncBuiltinESMExports();
process.env.HERMES_X402_TARGET = 'https://seller.example/paid';
process.env.HERMES_X402_EXPECTED_OFFER = JSON.stringify(expected);
async function cliFlow() {
  await fetch('https://seller.example/paid'); // CLI cannot reach its signer until this succeeds.
  signed++;
  await fetch('https://seller.example/paid', {headers:{'payment-signature':'signed'}});
}
(async () => {
  await import(process.argv[1]);
  for (const asHeader of [false, true]) {
    headerChallenge = asHeader;
    const before = calls;
    await assert.rejects(cliFlow(), /offer changed/);
    assert.equal(signed, 0);
    assert.equal(calls, before + 1); // No signed payment retry or RPC request.
  }
  offers = [swapped, {...expected, asset:expected.asset.toUpperCase().replace('0X','0x')}];
  const response = await fetch('https://seller.example/paid');
  const bound = await response.json();
  assert.equal(bound.accepts.length, 1);
  assert.equal(bound.accepts[0].payTo, expected.payTo);
  assert.equal(bound.resource.url, 'https://seller.example/paid');
  assert.equal(bound.extensions.unchanged, true);
  assert.deepEqual(JSON.parse(Buffer.from(response.headers.get('payment-required'), 'base64')), bound);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        proc = subprocess.run([NODE, '-e', script, guard], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_benchmark_reserves_budget_and_stops_on_unknown(self):
        with patch('template_x402.benchmark.x402_request', return_value={'outcome':'settlement_reported'}) as request:
            result = x402_benchmark({'url':self.params['url'], 'calls':3, 'budget':'0.001'})
        self.assertEqual(request.call_count, 3)
        self.assertLessEqual(x.Decimal(result['authorization_bound_sbc']), x.Decimal('0.001'))
        self.assertEqual(request.call_args.args[0]['max_amount'], '0.000333')
        with patch('template_x402.benchmark.x402_request', return_value={'outcome':'payment_unknown'}) as request:
            result = x402_benchmark({'url':self.params['url'], 'calls':3})
        self.assertEqual(result['calls_attempted'], 1)
        for budget in ['NaN', 'Infinity', '-1', '0.06']:
            with self.subTest(budget=budget), self.assertRaises(x.X402Error):
                x402_benchmark({'url':self.params['url'], 'budget':budget})


if __name__ == '__main__':
    unittest.main()
