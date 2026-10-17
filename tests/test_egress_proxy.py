"""Egress proxy (iron-proxy) secret injection: token derivation, agent-side
exports, proxy config, and a live swap through the real binary when present."""
from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "egress"))

import iron_egress as egress  # noqa: E402

SEED = "s" * 48
CA_CERT, CA_KEY = None, None


def _ca_pair():
    global CA_CERT, CA_KEY
    if CA_CERT is None:
        CA_CERT, CA_KEY = egress.generate_ca()
    return CA_CERT, CA_KEY


def agent_env(**extra):
    cert, _ = _ca_pair()
    env = {
        "EGRESS_PROXY_URL": "http://egress.railway.internal:9090",
        "EGRESS_TOKEN_SEED": SEED,
        "EGRESS_CA_CERT": cert,
        "EGRESS_SECRETS": "OPENROUTER_API_KEY,SLACK_BOT_TOKEN,SLACK_APP_TOKEN",
    }
    env.update(extra)
    return env


class TokenTests(unittest.TestCase):
    def test_tokens_are_deterministic_and_keep_client_prefixes(self):
        specs = {s.name: s for s in egress.managed_specs({"EGRESS_SECRETS": "SLACK_BOT_TOKEN,SLACK_APP_TOKEN,TELEGRAM_BOT_TOKEN"})}
        bot = egress.proxy_token(SEED.encode(), specs["SLACK_BOT_TOKEN"])
        self.assertEqual(bot, egress.proxy_token(SEED.encode(), specs["SLACK_BOT_TOKEN"]))
        self.assertTrue(bot.startswith("xoxb-hermes-egress-"))
        self.assertTrue(egress.proxy_token(SEED.encode(), specs["SLACK_APP_TOKEN"]).startswith("xapp-1-"))
        bot_id, _, rest = egress.proxy_token(SEED.encode(), specs["TELEGRAM_BOT_TOKEN"]).partition(":")
        self.assertTrue(bot_id.isdigit() and len(bot_id) == 10 and rest.startswith("hermes-egress-"))
        self.assertNotEqual(bot, egress.proxy_token(b"t" * 48, specs["SLACK_BOT_TOKEN"]))

    def test_short_seed_and_unknown_or_uncovered_names_are_refused(self):
        with self.assertRaisesRegex(egress.EgressError, "EGRESS_TOKEN_SEED"):
            egress._seed({"EGRESS_TOKEN_SEED": "short"})
        with self.assertRaisesRegex(egress.EgressError, "no injection rule"):
            egress.managed_specs({"EGRESS_SECRETS": "MYSTERY_KEY"})
        with self.assertRaisesRegex(egress.EgressError, "websocket"):
            egress.managed_specs({"EGRESS_SECRETS": "DISCORD_BOT_TOKEN"})

    def test_custom_secrets_and_host_overrides(self):
        specs = egress.managed_specs({
            "EGRESS_SECRETS": "ACME_TOKEN,OPENAI_API_KEY",
            "EGRESS_CUSTOM_SECRETS": json.dumps({"ACME_TOKEN": {"hosts": ["api.acme.test"], "headers": ["X-Acme"]}}),
            "EGRESS_HOSTS_OPENAI_API_KEY": "llm.internal.example",
        })
        self.assertEqual(specs[0].hosts, ("api.acme.test",))
        self.assertEqual(specs[0].headers, ("X-Acme",))
        self.assertEqual(specs[1].hosts, ("llm.internal.example",))


class GenCaTests(unittest.TestCase):
    def test_gen_ca_prints_seed_and_split_pem_pair(self):
        out = egress.gen_ca()
        shared, egress_only = out.split("# Egress service only:")
        self.assertIn("EGRESS_TOKEN_SEED=", shared)
        self.assertIn("BEGIN CERTIFICATE", shared)
        self.assertNotIn("PRIVATE KEY", shared)
        self.assertIn('EGRESS_CA_KEY="-----BEGIN PRIVATE KEY-----', egress_only)
        seed = shared.split("EGRESS_TOKEN_SEED=", 1)[1].split("\n", 1)[0]
        self.assertGreaterEqual(len(seed), egress.MIN_SEED_LENGTH)


class AgentSideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_exports_tokens_proxy_and_ca_bundle(self):
        exports, warnings = egress.client_env(agent_env(), self.state)
        self.assertEqual(warnings, [])
        self.assertIn(egress.TOKEN_MARKER, exports["OPENROUTER_API_KEY"])
        self.assertEqual(exports["HTTPS_PROXY"], "http://egress.railway.internal:9090")
        self.assertEqual(exports["HTTP_PROXY"], "http://egress.railway.internal:9091")
        self.assertEqual(exports["https_proxy"], exports["HTTPS_PROXY"])
        self.assertIn(".railway.internal", exports["NO_PROXY"].split(","))
        bundle = Path(exports["SSL_CERT_FILE"]).read_text()
        self.assertTrue(bundle.rstrip().endswith(_ca_pair()[0].strip()))
        self.assertEqual(Path(exports["NODE_EXTRA_CA_CERTS"]).read_text().strip(), _ca_pair()[0].strip())

    def test_real_secret_on_agent_service_refuses_boot(self):
        with self.assertRaisesRegex(egress.EgressError, "OPENROUTER_API_KEY still set to a real value"):
            egress.client_env(agent_env(OPENROUTER_API_KEY="sk-or-v1-REAL"), self.state)
        exports, warnings = egress.client_env(agent_env(OPENROUTER_API_KEY="sk-or-v1-REAL", EGRESS_ENFORCE="false"), self.state)
        self.assertNotIn("REAL", exports["OPENROUTER_API_KEY"])
        self.assertTrue(any("EGRESS_ENFORCE=false" in w for w in warnings))

    def test_token_already_present_is_accepted(self):
        first, _ = egress.client_env(agent_env(), self.state)
        again, warnings = egress.client_env(agent_env(OPENROUTER_API_KEY=first["OPENROUTER_API_KEY"]), self.state)
        self.assertEqual(again["OPENROUTER_API_KEY"], first["OPENROUTER_API_KEY"])
        self.assertEqual(warnings, [])

    def test_unmanaged_catalog_secret_is_reported(self):
        _, warnings = egress.client_env(agent_env(GITHUB_TOKEN="ghp_real"), self.state)
        self.assertTrue(any(w.startswith("GITHUB_TOKEN is set") for w in warnings))

    def test_private_key_in_cert_variable_is_refused(self):
        cert, key = _ca_pair()
        with self.assertRaisesRegex(egress.EgressError, "private key"):
            egress.client_env(agent_env(EGRESS_CA_CERT=cert + "\n" + key), self.state)

    def test_cli_writes_private_exports_file_not_stdout(self):
        env = {**os.environ, **agent_env(), "EGRESS_STATE_DIR": str(self.state)}
        out = self.state / "exports.sh"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/egress/iron_egress.py"), "client-env", str(out)],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")
        shown = subprocess.run(["bash", "-c", f'source "{out}"; printf "%s" "$SLACK_APP_TOKEN"'], text=True, capture_output=True)
        self.assertTrue(shown.stdout.startswith("xapp-1-hermes-egress-"))


class ProxySideTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def proxy_env(self, **extra):
        cert, key = _ca_pair()
        env = {
            "PATH": "/usr/bin", "EGRESS_TOKEN_SEED": SEED, "EGRESS_CA_CERT": cert, "EGRESS_CA_KEY": key,
            "EGRESS_SECRETS": "OPENROUTER_API_KEY,SLACK_BOT_TOKEN,SLACK_APP_TOKEN",
            "OPENROUTER_API_KEY": "sk-or-v1-real", "SLACK_BOT_TOKEN": "xoxb-real", "SLACK_APP_TOKEN": "xapp-real",
            "UNRELATED_SECRET": "do-not-forward",
        }
        env.update(extra)
        return env

    def test_config_and_minimal_daemon_env(self):
        cfg_path, daemon_env = egress.prepare_proxy(self.proxy_env(), self.state)
        self.assertEqual(oct(self.state.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.state / "ca.key").stat().st_mode & 0o777), "0o600")
        self.assertEqual(set(daemon_env), {"PATH", "HOME", "OPENROUTER_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"})
        text = cfg_path.read_text()
        self.assertNotIn("real", text)  # only token values and env var names
        cfg = yaml.safe_load(text)
        self.assertEqual(cfg["proxy"]["tunnel_listen"], "[::]:9090")
        self.assertEqual(cfg["proxy"]["http_listen"], "[::]:9091")
        self.assertIn("fc00::/7", cfg["proxy"]["upstream_deny_cidrs"])
        self.assertEqual(cfg["transforms"][0]["config"]["domains"], ["*"])
        rules = {r["source"]["var"]: r["replace"] for r in cfg["transforms"][1]["config"]["secrets"]}
        self.assertTrue(rules["OPENROUTER_API_KEY"]["require"])
        # Bot and app tokens share slack.com; two require rules would reject each other.
        self.assertFalse(rules["SLACK_BOT_TOKEN"]["require"])
        self.assertFalse(rules["SLACK_APP_TOKEN"]["require"])

    def test_explicit_allowlist_includes_secret_hosts(self):
        cfg_path, _ = egress.prepare_proxy(self.proxy_env(EGRESS_ALLOWED_HOSTS="grug402.dev"), self.state)
        domains = yaml.safe_load(cfg_path.read_text())["transforms"][0]["config"]["domains"]
        self.assertEqual(domains[0], "grug402.dev")
        self.assertIn("openrouter.ai", domains)
        self.assertIn("*.slack.com", domains)

    def test_missing_or_token_values_are_refused(self):
        env = self.proxy_env()
        del env["SLACK_APP_TOKEN"]
        with self.assertRaisesRegex(egress.EgressError, "missing real values for: SLACK_APP_TOKEN"):
            egress.prepare_proxy(env, self.state)
        spec = egress.CATALOG["OPENROUTER_API_KEY"]
        with self.assertRaisesRegex(egress.EgressError, "hold proxy tokens"):
            egress.prepare_proxy(self.proxy_env(OPENROUTER_API_KEY=egress.proxy_token(SEED.encode(), spec)), self.state)


class _Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _iron_proxy_bin():
    candidate = os.environ.get("IRON_PROXY_BIN") or shutil.which("iron-proxy") or "/usr/local/bin/iron-proxy"
    return candidate if os.access(candidate, os.X_OK) else None


@unittest.skipUnless(_iron_proxy_bin(), "iron-proxy binary not installed (set IRON_PROXY_BIN)")
class LiveIronProxyTests(unittest.TestCase):
    """Real binary: the token is swapped on the secret's host, and nowhere else."""

    def setUp(self):
        # iron-proxy cannot mint leaf certs for IP literals, so "another host"
        # is localhost with the secret bound to a different name.
        other = self._testMethodName == "test_token_not_swapped_on_other_hosts"
        self.secret_host = "api.acme.invalid" if other else "localhost"
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        # Upstream HTTPS echo server with a certificate the proxy trusts via SSL_CERT_FILE.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        import datetime
        import ipaddress
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        (tmp / "up.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp / "up.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tmp / "up.crt", tmp / "up.key")
        self.upstream.socket = ctx.wrap_socket(self.upstream.socket, server_side=True)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.up_port = self.upstream.server_address[1]

        self.port = _free_port()
        while self.port + 1 == self.up_port:
            self.port = _free_port()
        cert_pem, key_pem = _ca_pair()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(tmp),
            "SSL_CERT_FILE": str(tmp / "up.crt"),
            "EGRESS_TOKEN_SEED": SEED, "EGRESS_CA_CERT": cert_pem, "EGRESS_CA_KEY": key_pem,
            "EGRESS_SECRETS": "ACME_TOKEN",
            "EGRESS_CUSTOM_SECRETS": json.dumps({"ACME_TOKEN": {"hosts": [self.secret_host], "prefix": "acme_"}}),
            "ACME_TOKEN": "acme-REAL-VALUE",
            "EGRESS_PORT": str(self.port), "EGRESS_LISTEN_HOST": "127.0.0.1",
            "EGRESS_DENY_CIDRS": "",  # the upstream is on loopback
            "EGRESS_STATE_DIR": str(tmp / "state"), "IRON_PROXY_BIN": _iron_proxy_bin(),
        }
        self.token = egress.proxy_token(SEED.encode(), egress.managed_specs(env)[0])
        self.log = open(tmp / "proxy.log", "w")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "scripts/egress/iron_egress.py"), "proxy"],
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("iron-proxy did not start: " + (tmp / "proxy.log").read_text())
        self.ca_file = str(tmp / "state" / "ca.crt")

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.log.close()
        self.upstream.shutdown()
        self._tmp.cleanup()

    def fetch(self, host, auth):
        """CONNECT through the tunnel listener, as HTTPS_PROXY clients do."""
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10,
                                           context=ssl.create_default_context(cafile=self.ca_file))
        conn.set_tunnel(host, self.up_port)
        try:
            conn.request("GET", "/v1/echo", headers={"Authorization": auth})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, json.loads(data) if resp.status == 200 else None
        finally:
            conn.close()

    def test_token_swapped_on_secret_host(self):
        status, body = self.fetch("localhost", f"Bearer {self.token}")
        self.assertEqual(status, 200)
        self.assertEqual(body["headers"]["Authorization"], "Bearer acme-REAL-VALUE")

    def test_request_without_token_is_rejected_on_secret_host(self):
        status, _ = self.fetch("localhost", "Bearer some-other-credential")
        self.assertEqual(status, 403)

    def test_token_not_swapped_on_other_hosts(self):
        status, body = self.fetch("localhost", f"Bearer {self.token}")
        self.assertEqual(status, 200)
        self.assertEqual(body["headers"]["Authorization"], f"Bearer {self.token}")


class EntrypointWiringTests(unittest.TestCase):
    def test_role_switch_runs_before_agent_bootstrap(self):
        text = (ROOT / "scripts/entrypoint.sh").read_text()
        self.assertLess(text.index('HERMES_SERVICE_ROLE:-agent}" == "egress"'), text.index("mkdir -p \"${HERMES_HOME}\""))
        self.assertLess(text.index("iron_egress.py client-env"), text.index("if ! has_valid_provider_config"))

    def test_private_key_never_written_to_env_file(self):
        text = (ROOT / "scripts/entrypoint.sh").read_text()
        self.assertNotIn('echo "RADIUS_PRIVATE_KEY=', text)


if __name__ == "__main__":
    unittest.main()
