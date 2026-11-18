"""Offline payment-boundary tests: no wallet keys, RPC calls or fund transfers."""
import base64
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

    def test_no_operator_cap_uses_exact_quote_without_a_hidden_ceiling(self):
        atomic = str(2**256 - 1)
        expected = x.Decimal(atomic[:-6] + '.' + atomic[-6:])
        for setting in [None, '', 'unlimited']:
            with self.subTest(setting=setting), patch.dict(os.environ), \
                    patch.object(x, '_send', return_value=response(offers=[offer(amount=atomic)])), \
                    patch.object(x, '_pay_radius_cli', return_value={'status': 200}) as pay:
                if setting is None:
                    os.environ.pop('X402_MAX_PER_CALL', None)
                else:
                    os.environ['X402_MAX_PER_CALL'] = setting
                result = x.x402_request(self.params)
                self.assertEqual(result['outcome'], 'payment_unknown')
                pay.assert_called_once()
                self.assertEqual(pay.call_args.args[4], expected)
                self.assertEqual(result['amount_atomic'], atomic)

    def test_request_limit_still_applies_without_operator_cap(self):
        with patch.dict(os.environ, {'X402_MAX_PER_CALL': 'unlimited'}), \
                patch.object(x, '_send', return_value=response(offers=[offer(amount='20000')])), \
                patch.object(x, '_pay_radius_cli') as pay:
            result = x.x402_request({**self.params, 'max_amount': '0.01'})
        self.assertEqual(result['outcome'], 'refused')
        pay.assert_not_called()

    def test_unlimited_wallet_status_reports_no_cap(self):
        with patch.dict(os.environ, {'X402_MAX_PER_CALL': 'unlimited'}), \
                patch.object(x, '_run_radius_cli', return_value={}):
            self.assertIsNone(x.x402_wallet_status({})['max_per_call'])

    def test_benchmark_keeps_its_requested_budget_without_operator_cap(self):
        with patch.dict(os.environ, {'X402_MAX_PER_CALL': 'unlimited'}), \
                patch('template_x402.benchmark.x402_request', return_value={'outcome': 'settlement_reported'}) as request:
            result = x402_benchmark({'url': self.params['url'], 'calls': 2, 'budget': '0.04'})
        self.assertEqual(result['authorization_bound_sbc'], '0.040000')
        self.assertEqual(request.call_args.args[0]['max_amount'], '0.020000')

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

    def test_public_host_mode_quotes_unlisted_sellers_without_signing(self):
        with patch.dict(os.environ, {'X402_ALLOWED_HOSTS': '*'}), \
                patch.object(x, '_send', return_value=response()) as send, \
                patch.object(x, '_pay_radius_cli') as pay:
            result = x.x402_request({'url': 'https://new-seller.example/paid', 'dry_run': True})
        self.assertEqual(result['outcome'], 'quoted')
        send.assert_called_once()
        pay.assert_not_called()

    def test_public_host_mode_preserves_payment_cap(self):
        with patch.dict(os.environ, {'X402_ALLOWED_HOSTS': '*'}), \
                patch.object(x, '_send', return_value=response(offers=[offer(amount='20000')])), \
                patch.object(x, '_pay_radius_cli') as pay:
            result = x.x402_request({'url': 'https://new-seller.example/paid', 'max_amount': '1'})
        self.assertEqual(result['outcome'], 'refused')
        pay.assert_not_called()

    def test_public_host_mode_still_rejects_unsafe_urls_and_addresses(self):
        with patch.dict(os.environ, {'X402_ALLOWED_HOSTS': '*'}):
            for url in ['http://new-seller.example/', 'https://new-seller.example:8443/',
                        'https://user:pass@new-seller.example/', 'https://new-seller.example/#fragment']:
                with self.subTest(url=url), patch.object(x, '_send') as send:
                    self.assertEqual(x.x402_request({'url': url})['outcome'], 'error')
                    send.assert_not_called()
            for addresses in [['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'],
                              ['::1'], ['fd12::1'], ['8.8.8.8', '10.0.0.1']]:
                resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
                with self.subTest(addresses=addresses), \
                        patch.object(socket, 'getaddrinfo', return_value=resolved), \
                        patch.object(x, '_send') as send:
                    self.assertEqual(x.x402_request({'url': 'https://new-seller.example/'})['outcome'], 'error')
                    send.assert_not_called()

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
                        {'headers': {'payment-response': bad_receipt}},
                        {'headers': {'payment-response': 'malformed'}},
                        {'headers': {'X-Payment-Response': bad_receipt}}):
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

    def test_ledger_records_full_accepts_list_v2_body_and_header_and_v1(self):
        base = offer(network='eip155:8453', asset='0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', amount='5000', payTo='0x'+'2'*40)
        v2 = response(offers=[base, offer()])
        with patch.object(x, '_send', return_value=v2):
            result = x.x402_request({**self.params, 'dry_run': True})
        self.assertEqual(result['outcome'], 'quoted')
        self.assertEqual([a['network'] for a in result['accepts']], ['eip155:8453', x.NETWORK])
        self.assertEqual([a['asset_symbol'] for a in result['accepts']], ['USDC', 'SBC'])
        self.assertEqual(result['accepts'][0]['amount'], '5000')
        self.assertEqual(result['accepts'][0]['pay_to'], '0x'+'2'*40)
        rows = [json.loads(line) for line in x._ledger_path().read_text().splitlines()]
        self.assertEqual(rows[-1]['event'], 'completed')
        self.assertEqual(rows[-1]['accepts'], result['accepts'])
        self.assertNotIn('accepts', rows[0])  # intent row is written before any network access
        # v2 PAYMENT-REQUIRED header
        header = requests.Response()
        header.status_code = 402
        header._content = b''
        header.headers['PAYMENT-REQUIRED'] = base64.b64encode(json.dumps({'x402Version': 2, 'accepts': [offer()]}).encode()).decode()
        self.assertEqual(x._extract_accepts(header)[0]['network'], x.NETWORK)
        # v1 body: maxAmountRequired and short network names; payment is refused but offers are still logged
        v1 = requests.Response()
        v1.status_code = 402
        v1._content = json.dumps({'x402Version': 1, 'accepts': [{'scheme': 'exact', 'network': 'base', 'maxAmountRequired': '10000',
                                                                  'asset': '0x'+'3'*40, 'payTo': '0x'+'4'*40, 'extra': {'symbol': 'XYZ'}}]}).encode()
        with patch.object(x, '_send', return_value=v1), patch.object(x, '_pay_radius_cli') as pay:
            result = x.x402_request(self.params)
        pay.assert_not_called()
        self.assertEqual(result['outcome'], 'error')
        self.assertEqual(result['accepts'], [{'network': 'base', 'asset': '0x'+'3'*40, 'asset_symbol': 'XYZ',
                                              'amount': '10000', 'scheme': 'exact', 'pay_to': '0x'+'4'*40}])

    def test_accepts_capture_is_best_effort_and_never_blocks_payment(self):
        for junk in [b'not json', b'[]', b'{"accepts": "x"}', b'{"accepts": [1, null, "x"]}']:
            bad = requests.Response()
            bad.status_code = 402
            bad._content = junk
            with self.subTest(junk=junk):
                self.assertIsNone(x._extract_accepts(bad))
        # An unparseable challenge on a 402 is logged as accepts: null and does not change the outcome.
        garbage = requests.Response()
        garbage.status_code = 402
        garbage._content = b'not json'
        with patch.object(x, '_send', return_value=garbage):
            result = x.x402_request(self.params)
        self.assertIn('accepts', result)
        self.assertIsNone(result['accepts'])
        self.assertEqual(result['outcome'], 'error')
        # A valid challenge still pays normally with accepts recorded alongside.
        payment = {'paid': True, 'network': x.NETWORK, 'protocol': 'x402', 'scheme': 'exact', 'asset': x.ASSET,
                   'amountWei': '100', 'payTo': offer()['payTo'], 'txHash': '0x'+'a'*64}
        with patch.object(x, '_send', return_value=response()), \
                patch.object(x, '_pay_radius_cli', return_value={'status': 200, 'payment': payment, 'cli_exit_code': 0}):
            result = x.x402_request(self.params)
        self.assertEqual(result['outcome'], 'settlement_reported')
        self.assertEqual(len(result['accepts']), 1)
        too_many = response(offers=[offer(network=f'eip155:{i}') for i in range(40)])
        self.assertEqual(len(x._extract_accepts(too_many)), x.MAX_ACCEPTS)
        with patch.object(x, '_send', return_value=response(status=200)):
            self.assertNotIn('accepts', x.x402_request(self.params))

    def test_ledger_accepts_summary_counts_requests_and_tolerates_old_rows(self):
        x._append_ledger({'id': 'old', 'outcome': 'quoted'})
        x._append_ledger({'id': 'a', 'outcome': 'quoted', 'accepts': [
            {'network': 'eip155:8453', 'asset': '0xaa', 'asset_symbol': 'USDC'}, {'network': 'eip155:8453', 'asset': '0xaa', 'asset_symbol': 'USDC'},
            {'network': x.NETWORK, 'asset': x.ASSET, 'asset_symbol': 'SBC'}]})
        x._append_ledger({'id': 'b', 'outcome': 'quoted', 'accepts': [{'network': 'eip155:8453', 'asset': '0xaa', 'asset_symbol': 'USDC'}]})
        x._append_ledger({'id': 'c', 'outcome': 'error', 'accepts': None})
        summary = x.x402_ledger({})['accepts_summary']
        self.assertEqual(summary['requests_with_accepts'], 2)
        self.assertEqual(summary['by_network'], {'eip155:8453': 2, x.NETWORK: 1})
        self.assertEqual(summary['by_token'], {'eip155:8453:0xaa': 2, f'{x.NETWORK}:{x.ASSET.lower()}': 1})

    def test_accepts_summary_uses_asset_identity_and_ignores_missing_fields(self):
        first, second = '0x'+'a'*40, '0x'+'b'*40
        summary = x._summarize_accepts([
            {'accepts': [{'network': 'eip155:8453', 'asset': first, 'asset_symbol': 'USDC'},
                         {'network': 'eip155:8453', 'asset': second, 'asset_symbol': 'USDC'},
                         {'network': 'eip155:8453', 'asset': '0x'+'A'*40, 'asset_symbol': 'OTHER'},
                         {'network': None, 'asset': first}, {}, None]},
            {'accepts': [{'network': 'eip155:8453', 'asset': first, 'asset_symbol': 'RENAMED'},
                         {'network': 'solana:testnet', 'asset': 'CaseSensitiveMint'}]},
            {'accepts': [{}, {'network': ''}, {'network': None}]},
            {'accepts': [{'network': 'base', 'asset': None}]},
        ])
        self.assertEqual(summary['requests_with_accepts'], 3)
        self.assertEqual(summary['by_network'], {'eip155:8453': 2, 'base': 1, 'solana:testnet': 1})
        self.assertEqual(summary['by_token'], {f'eip155:8453:{first}': 2,
                                              f'eip155:8453:{second}': 1,
                                              'solana:testnet:CaseSensitiveMint': 1})

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

    def test_all_four_networks_quote_and_reconcile_the_selected_asset(self):
        with patch.dict(os.environ, {'X402_PAID_NETWORKS': ','.join(x.NETWORKS), 'X402_MAX_PER_CALL': 'unlimited'}):
            for name, network in x.NETWORKS.items():
                selected = offer(network=network['network'], asset=network['asset'], amount='2000000')
                payment = {'paid': True, 'network': network['network'], 'protocol': 'x402', 'scheme': 'exact',
                           'asset': network['asset'], 'amountWei': '2000000', 'payTo': selected['payTo'],
                           'txHash': '0x'+'a'*64}
                with self.subTest(network=name), patch.object(x, '_send', return_value=response(offers=[selected])), \
                        patch.object(x, '_pay_radius_cli', return_value={'status': 200, 'payment': payment, 'cli_exit_code': 0}) as pay:
                    quoted = x.x402_request({**self.params, 'network': name, 'dry_run': True})
                    self.assertEqual(quoted['outcome'], 'quoted')
                    self.assertEqual(quoted['asset_symbol'], network['symbol'])
                    self.assertEqual(quoted['amount'], '2')
                    pay.assert_not_called()
                    result = x.x402_request({**self.params, 'network': network['network']})
                    self.assertEqual(result['outcome'], 'settlement_reported')
                    self.assertEqual(result['network'], network['network'])
                    self.assertEqual(result['asset'], network['asset'])
                    self.assertEqual(result['explorer_url'], network['explorer']+'/tx/'+payment['txHash'])
                    self.assertEqual(pay.call_args.args[4], x.Decimal('2'))
                for mutation in [{'network': 'eip155:1'}, {'asset': '0x'+'9'*40}, {'amountWei': '2000001'}]:
                    with self.subTest(network=name, mutation=mutation), patch.object(x, '_send', return_value=response(offers=[selected])), \
                            patch.object(x, '_pay_radius_cli', return_value={'status': 200, 'payment': {**payment, **mutation}, 'cli_exit_code': 0}):
                        self.assertEqual(x.x402_request(self.params)['outcome'], 'payment_unknown')

    def test_network_pin_and_supported_offer_selection(self):
        base = offer(network=x.NETWORKS['base-mainnet']['network'], asset=x.NETWORKS['base-mainnet']['asset'])
        with patch.dict(os.environ, {'X402_PAID_NETWORKS': ','.join(x.NETWORKS)}):
            challenge = {'accepts': [offer(network='eip155:1'), base, offer()]}
            self.assertEqual(x._pick_requirement(challenge), base)
            self.assertEqual(x._pick_requirement(challenge, 'radius-testnet'), offer())
            bad_asset = {**base, 'asset': x.NETWORKS['base-sepolia']['asset']}
            self.assertEqual(x._pick_requirement({'accepts': [bad_asset, base]}), base)
            with self.assertRaises(x.X402Error):
                x._pick_requirement({'accepts': [bad_asset]})
            with self.assertRaises(x.X402Error):
                x._pick_requirement({'accepts': [base]}, 'radius-mainnet')
            self.assertEqual(x._network_name('base-testnet'), 'base-sepolia')

    def test_settlement_receipt_must_match_selected_network_and_transaction(self):
        spec = x.NETWORKS['base-mainnet']
        selected = offer(network=spec['network'], asset=spec['asset'])
        tx = '0x'+'a'*64
        payment = {'paid': True, 'network': spec['network'], 'protocol': 'x402', 'scheme': 'exact',
                   'asset': spec['asset'], 'amountWei': '100', 'payTo': selected['payTo'], 'txHash': tx}
        receipt = {'success': True, 'network': spec['network'], 'transaction': tx}
        for change in [{}, {'network': x.NETWORK}, {'transaction': '0x'+'b'*64}, {'network': None}]:
            encoded = base64.b64encode(json.dumps({**receipt, **change}).encode()).decode()
            result = {'status': 200, 'payment': payment, 'cli_exit_code': 0, 'headers': {'payment-response': encoded}}
            with self.subTest(change=change), patch.dict(os.environ, {'X402_PAID_NETWORKS': 'base-mainnet'}), \
                    patch.object(x, '_send', return_value=response(offers=[selected])), patch.object(x, '_pay_radius_cli', return_value=result):
                entry = x.x402_request(self.params)
                self.assertEqual(entry['outcome'], 'payment_unknown' if change else 'settlement_reported')

    def test_catalog_filters_network_or_facilitator_metadata(self):
        catalog = Mock(status_code=200)
        catalog.json.return_value = {'scenarios': [
            {'id': 'radius', 'facilitator': 'radius-testnet', 'compliant': True},
            {'id': 'base', 'network': 'eip155:8453', 'compliant': True}]}
        with patch.dict(os.environ, {'X402_PAID_NETWORKS': ','.join(x.NETWORKS)}), patch.object(x, '_send', return_value=catalog):
            for name, expected in [('radius-testnet', 'radius'), ('base-mainnet', 'base')]:
                self.assertEqual([s['id'] for s in x.x402_catalog({'network': name})['scenarios']], [expected])

    def test_disabled_or_invalid_networks_fail_before_network_or_signing(self):
        for settings, preferred in [('', None), ('unknown', None), ('radius-testnet', 'base-mainnet'),
                                    ('radius-testnet', 'eip155:723487')]:
            with self.subTest(settings=settings, preferred=preferred), patch.dict(os.environ, {'X402_PAID_NETWORKS': settings}), \
                    patch.object(x, '_send') as send, patch.object(x, '_pay_radius_cli') as pay:
                self.assertEqual(x.x402_request({**self.params, 'network': preferred})['outcome'], 'error')
                send.assert_not_called()
                pay.assert_not_called()

    def test_cli_routes_each_network_to_correct_environment_and_family(self):
        with patch.dict(os.environ, {'X402_PAID_NETWORKS': ','.join(x.NETWORKS)}):
            for name, network in x.NETWORKS.items():
                selected = offer(network=network['network'], asset=network['asset'])
                with self.subTest(network=name), patch.object(x, '_run_radius_cli', return_value={}) as run:
                    x._pay_radius_cli('GET', self.params['url'], {}, None, x.Decimal('0.0001'), selected)
                    args = run.call_args.args[0]
                    self.assertEqual(args[args.index('--network')+1], network['environment'])
                    self.assertEqual(args[args.index('--networks')+1], network['family'])
                    self.assertEqual(args[args.index('--threshold')+1], '0.0001')
                    if network['family'] == 'radius':
                        self.assertEqual(args[args.index('--rpc-url')+1], network['rpc'])
                        self.assertEqual(args[args.index('--sbc')+1], network['asset'])
                    else:
                        self.assertNotIn('--rpc-url', args)
                    self.assertNotIn('--approve-permit2', args)
                    self.assertEqual(run.call_args.kwargs['expected_offer'], selected)

    def test_subprocess_forces_base_rpc_overrides(self):
        with patch.dict(os.environ, {'RADIUS_BASE_RPC_URL': 'http://127.0.0.1', 'RADIUS_BASE_SEPOLIA_RPC_URL': 'https://evil.example'}), \
                patch.object(x.shutil, 'which', return_value='/usr/bin/radius-cli'), \
                patch.object(x.subprocess, 'run', return_value=Mock(stdout='{}', returncode=0)) as run:
            x._run_radius_cli(['wallet', 'address'])
        env = run.call_args.kwargs['env']
        self.assertEqual(env['RADIUS_BASE_RPC_URL'], 'https://mainnet.base.org')
        self.assertEqual(env['RADIUS_BASE_SEPOLIA_RPC_URL'], 'https://sepolia.base.org')

    def test_multinetwork_status_and_tool_schemas_match_enabled_policy(self):
        with patch.dict(os.environ, {'X402_PAID_NETWORKS': ','.join(x.NETWORKS), 'X402_MAX_PER_CALL': 'unlimited'}), \
                patch.object(x, '_run_radius_cli', return_value={}):
            status = x.x402_wallet_status({})
        self.assertIsNone(status['network'])
        self.assertIsNone(status['max_per_call'])
        self.assertTrue(status['mainnet_enabled'])
        self.assertEqual(status['paid_networks'], list(x.NETWORKS))
        self.assertEqual({n['network'] for n in status['networks']}, {n['network'] for n in x.NETWORKS.values()})
        ctx = Mock()
        x.register(ctx)
        definitions = {call.kwargs['name']: call.kwargs['schema'] for call in ctx.register_tool.call_args_list}
        for tool in ['x402_request', 'x402_catalog', 'x402_benchmark']:
            self.assertIn('base-mainnet', definitions[tool]['parameters']['properties']['network']['enum'])

    def test_base_benchmark_forwards_network_and_reports_usdc_budget(self):
        with patch.dict(os.environ, {'X402_PAID_NETWORKS': 'base-mainnet', 'X402_MAX_PER_CALL': 'unlimited'}), \
                patch('template_x402.benchmark.x402_request', return_value={'outcome': 'settlement_reported'}) as request:
            result = x402_benchmark({'url': self.params['url'], 'calls': 2, 'budget': '0.04', 'network': 'base-mainnet'})
        self.assertEqual(request.call_args.args[0]['network'], 'base-mainnet')
        self.assertEqual(result['asset_symbol'], 'USDC')
        self.assertEqual(result['authorization_bound'], '0.040000')
        self.assertNotIn('authorization_bound_sbc', result)

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
