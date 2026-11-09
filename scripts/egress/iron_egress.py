#!/usr/bin/env python3
"""Egress proxy with secret injection, built on iron-proxy (ironsh/iron-proxy).

Two Railway services run from this one image:

* the agent (``HERMES_SERVICE_ROLE`` unset) holds only opaque proxy tokens and
  sends all outbound HTTP(S) through the egress service;
* the egress service (``HERMES_SERVICE_ROLE=egress``) holds the real secrets
  and runs iron-proxy, which terminates TLS with a private CA and swaps each
  token for its real value, only on the hosts that secret belongs to.

Real values never enter the agent container, so they are never written to
``${HERMES_HOME}/.env`` and a prompt-injected ``printenv`` only finds tokens
that are worthless outside the private network.

Hermes ships the same idea as ``hermes egress`` for Docker terminal sandboxes,
but there the agent process itself still reads real keys from ``.env``. This
module splits the trust boundary across services instead.

Subcommands:
  client-env  agent side: write shell exports (tokens, proxy vars, CA bundle) to a file
  proxy       egress side: render proxy.yaml and exec iron-proxy
  gen-ca      print a fresh CA pair and token seed for Railway variables
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

IRON_PROXY_VERSION = "0.39.0"
DEFAULT_PORT = 9090
TOKEN_MARKER = "hermes-egress-"
MIN_SEED_LENGTH = 32

# Refused at connect time even for allowed hosts: loopback, link-local (cloud
# metadata), RFC1918, IPv6 ULA (Railway's private network is fd12::/16, so the
# proxy cannot be used to reach sibling services), IPv4-mapped IPv6, CGNAT and
# the RFC2544 benchmark range. Same list Hermes uses for `hermes egress`.
DEFAULT_DENY_CIDRS: Tuple[str, ...] = (
    "127.0.0.0/8", "::1/128",
    "169.254.0.0/16", "fe80::/10",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
    "::ffff:0:0/96",
    "100.64.0.0/10",
    "198.18.0.0/15",
)

HTTP_METHODS: Tuple[str, ...] = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

# The only non-secret variables iron-proxy inherits. Everything else on the
# egress service, including secrets not in EGRESS_SECRETS, is dropped.
DAEMON_INFRA_ENV: Tuple[str, ...] = ("PATH", "HOME", "TZ", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")

# Secrets that cannot be swapped by a header/path/query rewrite. They stay in
# the agent service and are reported at boot.
NOT_INJECTABLE: Dict[str, str] = {
    "DISCORD_BOT_TOKEN": "sent inside the Discord gateway websocket payload",
    "RADIUS_PRIVATE_KEY": "used locally for signing, never sent over HTTP",
    "WEBHOOK_SECRET": "verifies inbound webhooks",
    "JWT_API_KEY": "verifies inbound POST /token requests",
    "HERMES_API_KEY": "authenticates the local A2A bridge",
}


@dataclass(frozen=True)
class SecretSpec:
    name: str
    hosts: Tuple[str, ...]
    headers: Tuple[str, ...] = ("Authorization",)
    match_path: bool = False
    match_query: bool = False
    # Kept in front of the token so clients that sniff key formats still accept it.
    prefix: str = ""
    # Hosts the client also needs that never see the secret (added to an explicit allowlist).
    companions: Tuple[str, ...] = ()


def _spec(name, hosts, headers=("Authorization",), *, path=False, query=False, prefix="", companions=()):
    return SecretSpec(name, tuple(hosts), tuple(headers), path, query, prefix, tuple(companions))


CATALOG: Dict[str, SecretSpec] = {s.name: s for s in (
    _spec("OPENROUTER_API_KEY", ("openrouter.ai", "*.openrouter.ai"), prefix="sk-or-v1-"),
    _spec("OPENAI_API_KEY", ("api.openai.com",), prefix="sk-"),
    _spec("ANTHROPIC_API_KEY", ("api.anthropic.com",), ("x-api-key", "Authorization"), prefix="sk-ant-"),
    _spec("NOUS_API_KEY", ("inference.nousresearch.com",)),
    _spec("VOICE_TOOLS_OPENAI_KEY", ("api.openai.com",), prefix="sk-"),
    # Socket Mode's websocket runs on *.slack-edge.com with a one-time ticket, not the token.
    _spec("SLACK_BOT_TOKEN", ("slack.com", "*.slack.com"), prefix="xoxb-", companions=("*.slack-edge.com",)),
    _spec("SLACK_APP_TOKEN", ("slack.com", "*.slack.com"), prefix="xapp-1-", companions=("*.slack-edge.com",)),
    # Telegram puts the token in the URL path: /bot<token>/method.
    _spec("TELEGRAM_BOT_TOKEN", ("api.telegram.org",), (), path=True),
    _spec("GITHUB_TOKEN", ("api.github.com", "uploads.github.com")),
    _spec("LINEAR_API_KEY", ("api.linear.app",), prefix="lin_api_"),
    _spec("FIRECRAWL_API_KEY", ("api.firecrawl.dev",), prefix="fc-"),
    _spec("FAL_KEY", ("fal.run", "*.fal.run", "*.fal.ai")),
    _spec("ELEVENLABS_API_KEY", ("api.elevenlabs.io",), ("xi-api-key",)),
    _spec("BROWSERBASE_API_KEY", ("*.browserbase.com",), ("X-BB-API-Key",), query=True),
    # GoDaddy sends both in one header: "sso-key <key>:<secret>".
    _spec("GODADDY_API_KEY", ("api.godaddy.com", "api.ote-godaddy.com")),
    _spec("GODADDY_API_SECRET", ("api.godaddy.com", "api.ote-godaddy.com")),
)}


class EgressError(RuntimeError):
    pass


def _csv(value: Optional[str]) -> List[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def is_true(value: Optional[str], default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _custom_specs(env: Mapping[str, str]) -> Dict[str, SecretSpec]:
    """EGRESS_CUSTOM_SECRETS: JSON {"NAME": {"hosts": [...], "headers": [...],
    "path": bool, "query": bool, "prefix": str}}. Holds no secret values, so it
    is set on both services."""
    raw = (env.get("EGRESS_CUSTOM_SECRETS") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EgressError(f"EGRESS_CUSTOM_SECRETS is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EgressError("EGRESS_CUSTOM_SECRETS must be a JSON object keyed by env var name.")
    specs = {}
    for name, item in data.items():
        if not isinstance(item, dict) or not item.get("hosts"):
            raise EgressError(f"EGRESS_CUSTOM_SECRETS[{name}] needs a non-empty \"hosts\" list.")
        specs[name] = _spec(
            name, item["hosts"], item.get("headers", ("Authorization",)),
            path=bool(item.get("path")), query=bool(item.get("query")), prefix=str(item.get("prefix", "")),
        )
    return specs


def managed_specs(env: Mapping[str, str]) -> List[SecretSpec]:
    """The secrets named in EGRESS_SECRETS, with per-secret host overrides
    (EGRESS_HOSTS_<NAME>) applied. Must be identical on both services."""
    names = _csv(env.get("EGRESS_SECRETS"))
    if not names:
        raise EgressError("EGRESS_SECRETS is empty: list the secrets the egress service injects, e.g. OPENROUTER_API_KEY,SLACK_BOT_TOKEN.")
    known = {**CATALOG, **_custom_specs(env)}
    specs = []
    for name in dict.fromkeys(names):
        if name in NOT_INJECTABLE:
            raise EgressError(f"{name} cannot be injected by the egress proxy ({NOT_INJECTABLE[name]}).")
        if name not in known:
            raise EgressError(f"{name} has no injection rule. Add it to EGRESS_CUSTOM_SECRETS with its upstream hosts.")
        spec = known[name]
        if hosts := _csv(env.get(f"EGRESS_HOSTS_{name}")):
            spec = replace(spec, hosts=tuple(hosts))
        specs.append(spec)
    return specs


def _seed(env: Mapping[str, str]) -> bytes:
    seed = (env.get("EGRESS_TOKEN_SEED") or "").strip()
    if len(seed) < MIN_SEED_LENGTH:
        raise EgressError(f"EGRESS_TOKEN_SEED must be at least {MIN_SEED_LENGTH} characters; run `iron_egress.py gen-ca` to mint one.")
    return seed.encode()


def proxy_token(seed: bytes, spec: SecretSpec) -> str:
    """Deterministic, so both services derive the same token from the shared
    seed without the agent ever being sent a value."""
    digest = hmac.new(seed, f"iron-egress/v1/{spec.name}".encode(), hashlib.sha256).hexdigest()
    if spec.name == "TELEGRAM_BOT_TOKEN":
        # Telegram tokens look like "<numeric bot id>:<secret>"; keep that shape.
        return f"{int(digest[:12], 16) % 9_000_000_000 + 1_000_000_000}:{TOKEN_MARKER}{digest[12:52]}"
    return f"{spec.prefix}{TOKEN_MARKER}{digest[:40]}"


# --------------------------------------------------------------------- agent side

def _ca_pem(env: Mapping[str, str]) -> str:
    pem = (env.get("EGRESS_CA_CERT") or "").replace("\\n", "\n").strip()
    if "BEGIN CERTIFICATE" not in pem:
        raise EgressError("EGRESS_CA_CERT must hold the egress CA certificate (PEM).")
    if "PRIVATE KEY" in pem:
        raise EgressError("EGRESS_CA_CERT contains a private key. The key belongs on the egress service only (EGRESS_CA_KEY).")
    return pem + "\n"


def _system_bundle() -> str:
    for candidate in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if Path(candidate).is_file():
            return Path(candidate).read_text()
    try:
        import certifi
        return Path(certifi.where()).read_text()
    except Exception:
        return ""


def _http_proxy_url(https_url: str) -> str:
    """Plain-HTTP forwards go to the listener on port + 1."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(https_url)
    if parts.scheme != "http" or not parts.hostname:
        raise EgressError(f"EGRESS_PROXY_URL must look like http://egress.railway.internal:{DEFAULT_PORT}")
    port = (parts.port or DEFAULT_PORT) + 1
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return urlunsplit((parts.scheme, f"{host}:{port}", "", "", ""))


def client_env(env: Mapping[str, str], state_dir: Path) -> Tuple[Dict[str, str], List[str]]:
    """Exports for the agent service, plus warnings. Writes the CA files into
    ``state_dir``. Fails when a managed secret still has a real value here."""
    proxy_url = (env.get("EGRESS_PROXY_URL") or "").strip().rstrip("/")
    seed = _seed(env)
    specs = managed_specs(env)
    ca = _ca_pem(env)
    enforce = is_true(env.get("EGRESS_ENFORCE"), default=True)

    exports: Dict[str, str] = {}
    warnings: List[str] = []
    leaked = []
    for spec in specs:
        token = proxy_token(seed, spec)
        current = env.get(spec.name, "")
        if current and current != token:
            leaked.append(spec.name)
        exports[spec.name] = token
    if leaked:
        msg = (f"{', '.join(leaked)} still set to a real value on the agent service. Move "
               f"{'it' if len(leaked) == 1 else 'them'} to the egress service and delete "
               f"{'it' if len(leaked) == 1 else 'them'} here; the agent only needs EGRESS_SECRETS.")
        if enforce:
            raise EgressError(msg)
        warnings.append(msg + " Replaced with proxy tokens for this boot (EGRESS_ENFORCE=false).")

    managed = {s.name for s in specs}
    for name in CATALOG:
        if name not in managed and env.get(name):
            warnings.append(f"{name} is set on the agent service but not listed in EGRESS_SECRETS, so the real value is visible to the agent.")
    base_url = (env.get("OPENAI_BASE_URL") or "").strip()
    if "OPENAI_API_KEY" in managed and base_url and not env.get("EGRESS_HOSTS_OPENAI_API_KEY"):
        from urllib.parse import urlsplit
        if (urlsplit(base_url).hostname or "") != "api.openai.com":
            warnings.append("OPENAI_BASE_URL is not api.openai.com, so OPENAI_API_KEY is never swapped there. Set "
                            "EGRESS_HOSTS_OPENAI_API_KEY to that host on both services.")
    if env.get("DISCORD_BOT_TOKEN"):
        warnings.append("DISCORD_BOT_TOKEN stays on the agent service: Discord sends it inside the gateway websocket payload.")

    state_dir.mkdir(parents=True, exist_ok=True)
    ca_path, bundle_path = state_dir / "ca.crt", state_dir / "ca-bundle.crt"
    ca_path.write_text(ca)
    bundle_path.write_text(_system_bundle().rstrip("\n") + "\n" + ca)

    # Radius RPC and faucet carry no managed secret, and some chain tools (Foundry)
    # may not trust a MITM CA, so they connect directly.
    no_proxy = ",".join(dict.fromkeys(["localhost", "127.0.0.1", "::1", ".railway.internal", ".radiustech.xyz",
                                       *_csv(env.get("EGRESS_NO_PROXY"))]))
    http_proxy = _http_proxy_url(proxy_url)
    for key, value in {
        "HTTPS_PROXY": proxy_url, "HTTP_PROXY": http_proxy, "NO_PROXY": no_proxy,
        "SSL_CERT_FILE": str(bundle_path), "REQUESTS_CA_BUNDLE": str(bundle_path),
        "CURL_CA_BUNDLE": str(bundle_path), "NODE_EXTRA_CA_CERTS": str(ca_path),
    }.items():
        exports[key] = value
        if key.endswith("_PROXY"):
            exports[key.lower()] = value  # curl and some Go tools read only lowercase
    exports["HERMES_EGRESS_PROXY"] = "1"
    return exports, warnings


# --------------------------------------------------------------------- egress side

def build_proxy_config(
    env: Mapping[str, str], specs: List[SecretSpec], seed: bytes, *, ca_cert: Path, ca_key: Path,
) -> Dict:
    # Not PORT: Railway injects that for public networking, which this service must not use.
    port = int(env.get("EGRESS_PORT") or DEFAULT_PORT)
    listen_host = (env.get("EGRESS_LISTEN_HOST") or "[::]").strip()
    allowed = _csv(env.get("EGRESS_ALLOWED_HOSTS")) or ["*"]
    if allowed != ["*"]:
        for host in (h for s in specs for h in (*s.hosts, *s.companions)):
            if host not in allowed:
                allowed.append(host)
    deny = env.get("EGRESS_DENY_CIDRS")
    deny_cidrs = list(DEFAULT_DENY_CIDRS) if deny is None else _csv(deny)

    host_owners: Dict[str, int] = {}
    for host in (h for s in specs for h in s.hosts):
        host_owners[host] = host_owners.get(host, 0) + 1

    rules = []
    for spec in specs:
        rules.append({
            "source": {"type": "env", "var": spec.name},
            "replace": {
                "proxy_value": proxy_token(seed, spec),
                "match_headers": list(spec.headers),
                "match_path": spec.match_path,
                "match_query": spec.match_query,
                "match_body": False,
                # Fail closed: a request to this secret's host without its token
                # is rejected, so nothing can reach it with other credentials.
                # Two require rules on one host would reject each other's
                # requests, so shared hosts (Slack bot + app tokens) opt out.
                "require": all(host_owners[h] == 1 for h in spec.hosts),
            },
            # CONNECT runs through the pipeline too, before any header is
            # visible, so a require rule matching it would refuse every tunnel.
            # Matching only the inner requests keeps require meaningful.
            "rules": [{"host": h, "methods": list(HTTP_METHODS)} for h in spec.hosts],
        })

    allowlist: Dict = {"domains": allowed}
    if is_true(env.get("EGRESS_ALLOWLIST_WARN_ONLY")):
        allowlist["warn"] = True
    return {
        # Required by the parser; the DNS interception mode is not used.
        "dns": {"listen": "127.0.0.1:0", "proxy_ip": "127.0.0.1"},
        "proxy": {
            "tunnel_listen": f"{listen_host}:{port}",
            "http_listen": f"{listen_host}:{port + 1}",
            "https_listen": "127.0.0.1:0",
            "max_request_body_bytes": 16 * 1024 * 1024,
            "max_response_body_bytes": 0,
            "upstream_response_header_timeout": env.get("EGRESS_UPSTREAM_TIMEOUT") or "300s",
            "upstream_deny_cidrs": deny_cidrs,
        },
        # Default is :9090, which collides with the tunnel port.
        "metrics": {"listen": "127.0.0.1:0"},
        "tls": {"ca_cert": str(ca_cert), "ca_key": str(ca_key), "cert_cache_size": 1000, "leaf_cert_expiry_hours": 168},
        "transforms": [
            {"name": "allowlist", "config": allowlist},
            {"name": "secrets", "config": {"secrets": rules}},
        ],
        "log": {"level": env.get("EGRESS_LOG_LEVEL") or "info"},
    }


def _write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)


def prepare_proxy(env: Mapping[str, str], state_dir: Path) -> Tuple[Path, Dict[str, str]]:
    """Write CA files and proxy.yaml; return the config path and the minimal
    environment iron-proxy runs with (infrastructure vars + managed secrets)."""
    import yaml

    seed = _seed(env)
    specs = managed_specs(env)
    ca = _ca_pem(env)
    key = (env.get("EGRESS_CA_KEY") or "").replace("\\n", "\n").strip()
    if "PRIVATE KEY" not in key:
        raise EgressError("EGRESS_CA_KEY must hold the egress CA private key (PEM).")

    daemon_env = {k: env[k] for k in DAEMON_INFRA_ENV if env.get(k)}
    daemon_env.setdefault("HOME", str(state_dir))  # iron-proxy refuses to start without one
    missing, tokens = [], []
    for spec in specs:
        value = env.get(spec.name, "")
        if not value:
            missing.append(spec.name)
        elif value == proxy_token(seed, spec):
            tokens.append(spec.name)
        daemon_env[spec.name] = value
    if missing:
        raise EgressError(f"Egress service is missing real values for: {', '.join(missing)}.")
    if tokens:
        raise EgressError(f"{', '.join(tokens)} on the egress service hold proxy tokens, not real values.")

    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    ca_path, key_path, cfg_path = state_dir / "ca.crt", state_dir / "ca.key", state_dir / "proxy.yaml"
    ca_path.write_text(ca)
    _write_private(key_path, key + "\n")
    cfg = build_proxy_config(env, specs, seed, ca_cert=ca_path, ca_key=key_path)
    _write_private(cfg_path, yaml.safe_dump(cfg, sort_keys=False))
    return cfg_path, daemon_env


def generate_ca() -> Tuple[str, str]:
    """A fresh (certificate PEM, private key PEM) pair for the egress CA."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Radius Hermes egress CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode().strip()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode().strip()
    return cert_pem, key_pem


def gen_ca() -> str:
    """CA pair plus token seed, formatted for Railway's raw variable editor."""
    import secrets

    cert_pem, key_pem = generate_ca()
    return "\n".join([
        "# Both services (shared variables):",
        f"EGRESS_TOKEN_SEED={secrets.token_urlsafe(48)}",
        f'EGRESS_CA_CERT="{cert_pem}"',
        "",
        "# Egress service only:",
        f'EGRESS_CA_KEY="{key_pem}"',
    ]) + "\n"


def main(argv: List[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    env = dict(os.environ)
    try:
        if command == "client-env":
            # Exports go to a private file for the entrypoint to source, never
            # to stdout, so running this by hand can't put tokens in logs.
            if len(argv) != 3:
                print("usage: iron_egress.py client-env <exports-file>", file=sys.stderr)
                return 2
            state = Path(env.get("EGRESS_STATE_DIR") or Path(env.get("HERMES_HOME", "/data/.hermes")) / "egress")
            exports, warnings = client_env(env, state)
            for warning in warnings:
                print(f"[egress] WARNING: {warning}", file=sys.stderr)
            _write_private(Path(argv[2]), "".join(f"export {k}={shlex.quote(v)}\n" for k, v in exports.items()))
            return 0
        if command == "proxy":
            state = Path(env.get("EGRESS_STATE_DIR") or "/tmp/iron-egress")
            cfg_path, daemon_env = prepare_proxy(env, state)
            binary = env.get("IRON_PROXY_BIN") or "/usr/local/bin/iron-proxy"
            names = sorted(k for k in daemon_env if k not in DAEMON_INFRA_ENV)
            print(f"[egress] iron-proxy v{IRON_PROXY_VERSION} injecting {', '.join(names)}", flush=True)
            # execve, not a shell: secrets never appear in argv or a file.
            os.execve(binary, [binary, "-config", str(cfg_path)], daemon_env)
        if command == "gen-ca":
            sys.stdout.write(gen_ca())
            return 0
    except EgressError as exc:
        print(f"[egress] ERROR: {exc}", file=sys.stderr)
        return 1
    print(__doc__.split("Subcommands:", 1)[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
