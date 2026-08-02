"""Canonical KYA / PAY / KYA-PAY verification + token-issuance module.

This is the single source of truth for KYA spec handling in this template.
It is intentionally framework-agnostic so it can be imported from:

  - ``plugins/kya-spec/`` (provides ``kya_validate_claims`` to the LLM)
  - ``scripts/agent_server/main.py`` (A2A inbound middleware + JWKS endpoint)
  - ``plugins/kya-mint/`` (provides ``generate_kya_token`` to the LLM)

Spec reference: https://kyapay.org/specifications

Design notes
------------
- Signature verification uses ``pyjwt[crypto]`` (already a project dep). We
  hand a JWK to ``ECAlgorithm.from_jwk`` and let pyjwt decode — we never
  hand-roll P1363 vs DER signature encoding.
- ES256 only. The public spec says "currently ES256". ES256K is *not* a
  drop-in substitute because the curves differ (P-256 vs secp256k1).
- The agent's Radius wallet key is secp256k1 and cannot be reused for
  KYA. Outbound KYA minting uses a separate persistent ES256/P-256
  keypair stored under ``${HERMES_HOME}/.radius/kya/``.
- JWKS for our own issuer is generated at startup time from that keypair
  and served at ``GET /.well-known/jwks.json``.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


# ---------------------------------------------------------------------------
# Spec constants — sourced from https://kyapay.org/specifications
# ---------------------------------------------------------------------------

GENERAL_REQUIRED_HEADER = {"alg", "kid", "typ"}
GENERAL_REQUIRED_PAYLOAD = {"iss", "sub", "aud", "iat", "jti", "exp"}
TYP_ALLOWED = {"kya+jwt", "pay+jwt", "kya-pay+jwt"}

SUPPORTED_JWA = {"ES256"}

HID_REQUIRED_WHEN_PRESENT = {"email"}
APD_REQUIRED_WHEN_PRESENT = {"id", "name"}
AID_REQUIRED = {"name", "creation_ip"}
STP_KNOWN = {"coin", "card"}

DEFAULT_CLOCK_SKEW = 60
DEFAULT_JTI_CACHE_SIZE = 2048
DEFAULT_JWKS_TTL = 3600
DEFAULT_HTTP_TIMEOUT = 15
DEFAULT_TOKEN_TTL = 3600  # spec is silent; one hour is a sane default

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# In-memory caches
# ---------------------------------------------------------------------------


class _JWKSCache:
    def __init__(self, ttl: int = DEFAULT_JWKS_TTL):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._store: dict[str, tuple[dict[str, Any], float]] = {}

    def get(self, issuer: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._store.get(issuer)
            if not entry:
                return None
            jwks, expires_at = entry
            if time.time() >= expires_at:
                self._store.pop(issuer, None)
                return None
            return jwks

    def put(self, issuer: str, jwks: dict[str, Any]) -> None:
        with self._lock:
            self._store[issuer] = (jwks, time.time() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class _JTILRU:
    def __init__(self, maxsize: int = DEFAULT_JTI_CACHE_SIZE):
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._store: "OrderedDict[tuple[str, str], float]" = OrderedDict()

    def check_and_add(self, issuer: str, jti: str, exp: int) -> bool:
        key = (issuer, jti)
        now = time.time()
        with self._lock:
            stale = [k for k, e in self._store.items() if e <= now]
            for k in stale:
                self._store.pop(k, None)
            if key in self._store:
                self._store.move_to_end(key)
                return False
            self._store[key] = float(exp)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# Module-level singletons. Tests reset these via ``reset_caches()``.
_JWKS = _JWKSCache()
_SEEN_JTI = _JTILRU()


def reset_caches() -> None:
    """Reset module-level JWKS and jti caches (test hook)."""
    global _JWKS, _SEEN_JTI
    _JWKS = _JWKSCache()
    _SEEN_JTI = _JTILRU()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: int = DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    _validate_outbound_jwks_url(url)

    req = Request(
        url,
        headers={
            "Accept": "application/json,application/jwk-set+json;q=0.9,*/*;q=0.1",
            "User-Agent": "hermes-kya/2.0",
        },
    )

    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = build_opener(_NoRedirect)
    with opener.open(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read().decode(charset, errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object from {url}, got {type(data).__name__}")
    return data


def _validate_outbound_jwks_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("JWKS URL must use https")
    host = parsed.hostname
    if not host:
        raise ValueError("JWKS URL has no hostname")

    try:
        addrinfo = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except Exception as err:
        raise ValueError(f"Unable to resolve JWKS host '{host}': {err}") from err

    for _family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        ip_raw = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_raw)
        except ValueError as err:
            raise ValueError(f"Resolved host '{host}' to invalid IP '{ip_raw}': {err}") from err
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Resolved host '{host}' to disallowed non-public IP '{ip}' for JWKS fetch"
            )


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_split(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format: expected header.payload.signature")
    try:
        header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    except Exception as err:
        raise ValueError(f"Invalid JWT encoding: {err}") from err
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("JWT header and payload must each be a JSON object")
    return header, payload


def jwks_url_for_issuer(issuer: str) -> str:
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("Issuer must be a non-empty string")
    if not (issuer.startswith("https://") or issuer.startswith("http://")):
        raise ValueError(f"Issuer '{issuer}' must be an http(s) URL")
    return issuer.rstrip("/") + "/.well-known/jwks.json"


def _select_jwk(jwks: dict[str, Any], kid: str) -> dict[str, Any]:
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        raise ValueError("JWKS document has no 'keys' array")
    for key in keys:
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    raise ValueError(f"No JWK matched header kid '{kid}'")


def _issuer_allowed(issuer: str, trusted_issuers: set[str]) -> bool:
    issuer_norm = issuer.strip().rstrip("/").lower()
    parsed = urlsplit(issuer_norm)
    host = parsed.hostname or ""
    scheme = parsed.scheme

    for raw in trusted_issuers:
        pattern = str(raw).strip().rstrip("/").lower()
        if not pattern:
            continue

        if pattern.startswith("https://*.") or pattern.startswith("http://*."):
            pattern_parsed = urlsplit(pattern)
            suffix = (pattern_parsed.hostname or "")
            if suffix.startswith("*."):
                suffix = suffix[2:]
            if (
                pattern_parsed.scheme == scheme
                and host.endswith("." + suffix)
                and host != suffix
            ):
                return True
            continue

        if pattern.startswith("*."):
            suffix = pattern[2:]
            if host.endswith("." + suffix) and host != suffix:
                return True
            continue

        if "://" not in pattern:
            if host == pattern:
                return True
            continue

        if issuer_norm == pattern:
            return True

    return False


def _verify_signature(token: str, jwk: dict[str, Any], expected_alg: str) -> None:
    import jwt as pyjwt
    from jwt.algorithms import ECAlgorithm

    if expected_alg not in SUPPORTED_JWA:
        raise ValueError(f"Algorithm '{expected_alg}' is not in SUPPORTED_JWA {sorted(SUPPORTED_JWA)}")
    try:
        public_key = ECAlgorithm.from_jwk(json.dumps(jwk))
    except Exception as err:
        raise ValueError(f"Unable to load JWK as EC public key: {err}") from err
    try:
        pyjwt.decode(
            token,
            public_key,
            algorithms=[expected_alg],
            options={
                "verify_signature": True,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "verify_aud": False,
                "verify_iss": False,
                "require": [],
            },
        )
    except pyjwt.InvalidSignatureError as err:
        raise ValueError(f"JWT signature verification failed: {err}") from err
    except pyjwt.InvalidAlgorithmError as err:
        raise ValueError(f"JWT algorithm not accepted: {err}") from err
    except pyjwt.DecodeError as err:
        raise ValueError(f"JWT decode failed: {err}") from err


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationReport:
    """Plain container; not a dataclass to remain robust under importlib loading."""

    __slots__ = ("ok", "token_type", "issuer", "kid", "signature_verified", "errors", "warnings")

    def __init__(self, token_type: str = "") -> None:
        self.ok: bool = False
        self.token_type: str = token_type
        self.issuer: str | None = None
        self.kid: str | None = None
        self.signature_verified: bool = False
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "token_type": self.token_type,
            "issuer": self.issuer,
            "kid": self.kid,
            "signature_verified": self.signature_verified,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def _validate_timestamps(payload: dict[str, Any], errors: list[str], clock_skew: int) -> None:
    now = int(time.time())
    iat = payload.get("iat")
    if isinstance(iat, (int, float)) and not isinstance(iat, bool):
        if int(iat) > now + clock_skew:
            errors.append("Claim 'iat' is in the future beyond allowed clock skew.")
    else:
        errors.append("Claim 'iat' must be a numeric Unix timestamp.")
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        if int(exp) <= now - clock_skew:
            errors.append("Claim 'exp' indicates the token is expired.")
    else:
        errors.append("Claim 'exp' must be a numeric Unix timestamp.")


def _validate_kya_subclaims(payload: dict[str, Any], errors: list[str]) -> None:
    aid = payload.get("aid")
    if not isinstance(aid, dict):
        errors.append("Claim 'aid' must be an object.")
    else:
        for required in AID_REQUIRED:
            if required not in aid:
                errors.append(f"Claim 'aid.{required}' is required.")
        creation_ip = aid.get("creation_ip")
        if isinstance(creation_ip, str) and creation_ip and not _is_public_ip(creation_ip):
            errors.append("Claim 'aid.creation_ip' must be a public IPv4 or IPv6 address.")
    hid = payload.get("hid")
    if hid is not None:
        if not isinstance(hid, dict):
            errors.append("Claim 'hid' must be an object when present.")
        else:
            for required in HID_REQUIRED_WHEN_PRESENT:
                if required not in hid:
                    errors.append(f"Claim 'hid.{required}' is required when 'hid' is present.")
    apd = payload.get("apd")
    if apd is not None:
        if not isinstance(apd, dict):
            errors.append("Claim 'apd' must be an object when present.")
        else:
            for required in APD_REQUIRED_WHEN_PRESENT:
                if required not in apd:
                    errors.append(f"Claim 'apd.{required}' is required when 'apd' is present.")


def _validate_pay_subclaims(payload: dict[str, Any], errors: list[str], warnings: list[str]) -> None:
    stp = payload.get("stp")
    if stp is not None and stp not in STP_KNOWN:
        warnings.append(
            f"Claim 'stp'='{stp}' is not in the public spec's known set {sorted(STP_KNOWN)}."
        )
    sti = payload.get("sti")
    if sti is not None and not isinstance(sti, dict):
        errors.append("Claim 'sti' must be an object when present.")


def _validate_shape(
    token_type: str,
    header: dict[str, Any],
    payload: dict[str, Any],
    expected_audience: str | None,
    expected_environment: str | None,
    trusted_issuers: set[str] | None,
    clock_skew: int,
    enforce_audience: bool,
) -> ValidationReport:
    report = ValidationReport(token_type=token_type)
    errors = report.errors
    warnings = report.warnings

    missing_header = sorted(k for k in GENERAL_REQUIRED_HEADER if k not in header)
    if missing_header:
        errors.append(f"Missing required header claim(s): {', '.join(missing_header)}")

    alg = header.get("alg")
    if isinstance(alg, str) and alg and alg not in SUPPORTED_JWA:
        errors.append(
            f"Unsupported JWT algorithm '{alg}'. Expected one of: {sorted(SUPPORTED_JWA)}"
        )

    typ = header.get("typ")
    if isinstance(typ, str) and typ and typ not in TYP_ALLOWED:
        errors.append(f"Header 'typ' must be one of {sorted(TYP_ALLOWED)}, got '{typ}'")

    expected_typ = {"kya": "kya+jwt", "pay": "pay+jwt", "kya-pay": "kya-pay+jwt"}[token_type]
    if isinstance(typ, str) and typ and typ != expected_typ:
        errors.append(f"token_type='{token_type}' requires header typ='{expected_typ}', got '{typ}'")

    missing_payload = sorted(k for k in GENERAL_REQUIRED_PAYLOAD if k not in payload)
    if missing_payload:
        errors.append(f"Missing required payload claim(s): {', '.join(missing_payload)}")

    jti = payload.get("jti")
    if jti is not None:
        if not isinstance(jti, str) or not UUID_RE.match(jti):
            errors.append("Claim 'jti' must be a UUID string.")

    iss = payload.get("iss")
    if iss is not None and not (isinstance(iss, str) and (iss.startswith("https://") or iss.startswith("http://"))):
        errors.append("Claim 'iss' must be an http(s) URL.")
    report.issuer = iss if isinstance(iss, str) else None

    if trusted_issuers is not None and isinstance(iss, str) and not _issuer_allowed(iss, trusted_issuers):
        errors.append(f"Issuer '{iss}' is not in the trusted-issuer allowlist.")

    aud = payload.get("aud")
    if expected_audience is not None:
        if aud != expected_audience:
            errors.append(
                f"Claim 'aud'='{aud}' does not match expected_audience='{expected_audience}'."
            )
    elif enforce_audience:
        errors.append(
            "expected_audience was not provided; per the spec, recipients MUST validate 'aud'. "
            "Pass expected_audience or set enforce_audience=false explicitly to disable."
        )

    if expected_environment is not None:
        if payload.get("env") != expected_environment:
            errors.append(
                f"Claim 'env'='{payload.get('env')}' does not match expected_environment='{expected_environment}'."
            )

    _validate_timestamps(payload, errors, clock_skew)

    if token_type in ("kya", "kya-pay"):
        _validate_kya_subclaims(payload, errors)
    if token_type in ("pay", "kya-pay"):
        _validate_pay_subclaims(payload, errors, warnings)

    report.kid = header.get("kid") if isinstance(header.get("kid"), str) else None
    report.ok = not errors
    return report


def verify_token(
    *,
    token_type: str,
    token: str,
    expected_audience: str | None = None,
    expected_environment: str | None = None,
    trusted_issuers: set[str] | None = None,
    clock_skew: int = DEFAULT_CLOCK_SKEW,
    enforce_audience: bool = True,
    enforce_replay_protection: bool = True,
    jwks_override: dict[str, Any] | None = None,
) -> ValidationReport:
    """End-to-end verification.

    Order of operations:
      1. Decode header + payload.
      2. Run claim-shape validation (collects errors; does not short-circuit).
      3. If iss/kid/alg are usable, fetch JWKS and verify the signature.
      4. If signature is verified, check (iss, jti) against the replay LRU.
    """
    if token_type not in {"kya", "pay", "kya-pay"}:
        raise ValueError(f"Invalid token_type='{token_type}'")

    header, payload = jwt_split(token)

    report = _validate_shape(
        token_type=token_type,
        header=header,
        payload=payload,
        expected_audience=expected_audience,
        expected_environment=expected_environment,
        trusted_issuers=trusted_issuers,
        clock_skew=clock_skew,
        enforce_audience=enforce_audience,
    )

    issuer = payload.get("iss")
    kid = header.get("kid")
    alg = header.get("alg")

    can_verify = (
        isinstance(issuer, str)
        and isinstance(kid, str)
        and isinstance(alg, str)
        and alg in SUPPORTED_JWA
    )
    if can_verify:
        try:
            if jwks_override is not None:
                jwks = jwks_override
            else:
                jwks = _JWKS.get(issuer)
                if jwks is None:
                    jwks = _http_get_json(jwks_url_for_issuer(issuer))
                    _JWKS.put(issuer, jwks)
            jwk = _select_jwk(jwks, kid)
            _verify_signature(token, jwk, alg)
            report.signature_verified = True
        except Exception as err:
            report.errors.append(f"Signature verification failed: {err}")
            report.signature_verified = False
    else:
        report.errors.append("Cannot verify signature: missing or invalid iss/kid/alg in token.")

    if enforce_replay_protection and report.signature_verified:
        jti = payload.get("jti")
        exp = payload.get("exp")
        if isinstance(jti, str) and isinstance(exp, (int, float)):
            if not _SEEN_JTI.check_and_add(str(issuer), jti, int(exp)):
                report.errors.append(
                    f"Replay detected: jti='{jti}' for issuer '{issuer}' was already seen."
                )

    report.ok = report.signature_verified and not report.errors
    return report


# ---------------------------------------------------------------------------
# Token issuance (outbound KYA minting)
# ---------------------------------------------------------------------------
#
# KYA mandates ES256 (P-256). The agent's Radius wallet uses secp256k1, so
# we cannot reuse it. Instead, we manage a separate persistent P-256
# keypair under ``${HERMES_HOME}/.radius/kya/`` and serve its public half
# via the JWKS endpoint described below.


_KYA_KEY_LOCK = threading.Lock()
_KYA_PRIVATE_KEY = None  # cryptography.hazmat.primitives.asymmetric.ec.EllipticCurvePrivateKey


def _kya_key_dir() -> Path:
    base = os.environ.get("HERMES_HOME") or "/data/.hermes"
    return Path(base) / ".radius" / "kya"


def _kid_from_public_key(public_key) -> str:
    """Derive a stable kid from the EC public key (SHA-256 of compressed point, base64url)."""
    import hashlib
    from cryptography.hazmat.primitives import serialization

    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    digest = hashlib.sha256(pub_bytes).digest()
    return _b64url_encode(digest)[:32]


def _public_key_to_jwk(public_key, kid: str) -> dict[str, Any]:
    nums = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "kid": kid,
        "alg": "ES256",
        "use": "sig",
        "x": _b64url_encode(nums.x.to_bytes(32, "big")),
        "y": _b64url_encode(nums.y.to_bytes(32, "big")),
    }


def setup_kya_key() -> tuple[str, dict[str, Any]]:
    """Load or generate the agent's KYA signing keypair.

    Returns ``(kid, jwks)`` where ``jwks`` is the public JWKS document
    suitable for serving at ``/.well-known/jwks.json``.

    Persistence: PEM private key at ``${HERMES_HOME}/.radius/kya/key.pem``
    with file mode 0600. If absent, a fresh P-256 key is generated.
    """
    global _KYA_PRIVATE_KEY

    with _KYA_KEY_LOCK:
        if _KYA_PRIVATE_KEY is not None:
            public_key = _KYA_PRIVATE_KEY.public_key()
            kid = _kid_from_public_key(public_key)
            jwks = {"keys": [_public_key_to_jwk(public_key, kid)]}
            return kid, jwks

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ec import (
            SECP256R1,
            generate_private_key,
        )

        key_dir = _kya_key_dir()
        key_path = key_dir / "key.pem"
        private_key = None

        if key_path.exists():
            try:
                with open(key_path, "rb") as fh:
                    private_key = serialization.load_pem_private_key(fh.read(), password=None)
                # Sanity-check curve.
                from cryptography.hazmat.primitives.asymmetric.ec import (
                    EllipticCurvePrivateKey,
                )

                if not isinstance(private_key, EllipticCurvePrivateKey) or not isinstance(
                    private_key.curve, SECP256R1
                ):
                    raise ValueError("KYA key on disk is not a P-256 EC key; regenerating")
            except Exception:
                private_key = None

        if private_key is None:
            private_key = generate_private_key(SECP256R1())
            key_dir.mkdir(parents=True, exist_ok=True)
            pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            # Atomic write with restrictive perms.
            tmp_path = key_path.with_suffix(".pem.tmp")
            with open(tmp_path, "wb") as fh:
                fh.write(pem)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, key_path)

        public_key = private_key.public_key()
        kid = _kid_from_public_key(public_key)
        jwks = {"keys": [_public_key_to_jwk(public_key, kid)]}

        _KYA_PRIVATE_KEY = private_key
        return kid, jwks


def get_kya_jwks() -> dict[str, Any]:
    """Return the JWKS document for this agent's KYA issuer.

    Lazily initializes the keypair on first call.
    """
    _, jwks = setup_kya_key()
    return jwks


def get_kya_kid() -> str:
    kid, _ = setup_kya_key()
    return kid


def mint_token(
    *,
    token_type: str,
    issuer: str,
    subject: str,
    audience: str,
    aid: dict[str, Any] | None = None,
    hid: dict[str, Any] | None = None,
    apd: dict[str, Any] | None = None,
    pay: dict[str, Any] | None = None,
    env: str | None = None,
    seller_domain: str | None = None,
    originator: str | None = None,
    seller_service_id: str | None = None,
    buyer_tag: str | None = None,
    extra_claims: dict[str, Any] | None = None,
    ttl_seconds: int = DEFAULT_TOKEN_TTL,
) -> dict[str, Any]:
    """Mint a signed KYA/PAY/KYA-PAY JWT.

    Returns ``{"token", "header", "payload", "kid", "jwks_url"}``. Raises
    ``ValueError`` for invalid inputs (e.g. missing aid for a KYA token).
    """
    import jwt as pyjwt

    if token_type not in {"kya", "pay", "kya-pay"}:
        raise ValueError(f"Invalid token_type='{token_type}'")
    if not (isinstance(issuer, str) and (issuer.startswith("https://") or issuer.startswith("http://"))):
        raise ValueError("issuer must be an http(s) URL")
    if not isinstance(audience, str) or not audience:
        raise ValueError("audience is required")
    if not isinstance(subject, str) or not subject:
        raise ValueError("subject is required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    kid, _jwks = setup_kya_key()
    assert _KYA_PRIVATE_KEY is not None  # for type-checkers

    now = int(time.time())
    typ = {"kya": "kya+jwt", "pay": "pay+jwt", "kya-pay": "kya-pay+jwt"}[token_type]
    headers = {"alg": "ES256", "kid": kid, "typ": typ}

    payload: dict[str, Any] = {
        "iss": issuer.rstrip("/"),
        "sub": subject,
        "aud": audience,
        "iat": now,
        "exp": now + int(ttl_seconds),
        "jti": str(uuid.uuid4()),
    }
    if env is not None:
        payload["env"] = env
    if seller_domain is not None:
        payload["sdm"] = seller_domain
    if originator is not None:
        payload["ori"] = originator
    if seller_service_id is not None:
        payload["ssi"] = seller_service_id
    if buyer_tag is not None:
        payload["btg"] = buyer_tag

    if token_type in {"kya", "kya-pay"}:
        if not isinstance(aid, dict):
            raise ValueError("aid object is required for kya / kya-pay tokens")
        for required in AID_REQUIRED:
            if required not in aid:
                raise ValueError(f"aid.{required} is required")
        creation_ip = aid.get("creation_ip")
        if isinstance(creation_ip, str) and creation_ip and not _is_public_ip(creation_ip):
            raise ValueError("aid.creation_ip must be a public IPv4 or IPv6 address")
        payload["aid"] = aid
        if hid is not None:
            if not isinstance(hid, dict):
                raise ValueError("hid must be an object when provided")
            for required in HID_REQUIRED_WHEN_PRESENT:
                if required not in hid:
                    raise ValueError(f"hid.{required} is required when hid is provided")
            payload["hid"] = hid
        if apd is not None:
            if not isinstance(apd, dict):
                raise ValueError("apd must be an object when provided")
            for required in APD_REQUIRED_WHEN_PRESENT:
                if required not in apd:
                    raise ValueError(f"apd.{required} is required when apd is provided")
            payload["apd"] = apd

    if token_type in {"pay", "kya-pay"}:
        if not isinstance(pay, dict):
            raise ValueError("pay object is required for pay / kya-pay tokens")
        # Merge PAY-specific claims at the payload root, as the public examples show.
        for key in ("amt", "cur", "stp", "sti"):
            if key in pay:
                payload[key] = pay[key]
        sti = payload.get("sti")
        if sti is not None and not isinstance(sti, dict):
            raise ValueError("pay.sti must be an object when provided")

    if extra_claims:
        if not isinstance(extra_claims, dict):
            raise ValueError("extra_claims must be an object")
        # Don't allow overrides of required/known claims.
        reserved = (
            GENERAL_REQUIRED_PAYLOAD
            | {"env", "sdm", "ori", "ssi", "btg", "aid", "hid", "apd", "amt", "cur", "stp", "sti"}
        )
        for key in extra_claims:
            if key in reserved:
                raise ValueError(f"extra_claims may not override reserved claim '{key}'")
        payload.update(extra_claims)

    token = pyjwt.encode(payload, _KYA_PRIVATE_KEY, algorithm="ES256", headers=headers)
    return {
        "token": token,
        "header": headers,
        "payload": payload,
        "kid": kid,
        "jwks_url": jwks_url_for_issuer(issuer),
    }


# ---------------------------------------------------------------------------
# Inbound middleware helper
# ---------------------------------------------------------------------------


def parse_trusted_issuers(value: Any) -> set[str] | None:
    """Build a trusted-issuer set from explicit input or the env var."""
    if value is None:
        env = os.environ.get("TRUSTED_KYA_ISSUERS", "")
        if not env.strip():
            return None
        return {item.strip() for item in env.split(",") if item.strip()}
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
        return set(items) if items else None
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return set(items) if items else None
    raise ValueError("trusted_issuers must be a comma-separated string, list, or null")


def inbound_policy() -> str:
    """Return the configured KYA inbound policy: 'off', 'opportunistic', or 'required'."""
    raw = (os.environ.get("KYA_INBOUND_POLICY") or "off").strip().lower()
    if raw in {"off", "disabled", "false", "0"}:
        return "off"
    if raw in {"opportunistic", "optional"}:
        return "opportunistic"
    if raw in {"required", "enforce", "strict"}:
        return "required"
    return "off"


def inbound_header_name() -> str:
    """The custom HTTP header KYA uses to carry tokens (spec: skyfire-pay-id)."""
    return (os.environ.get("KYA_INBOUND_HEADER") or "skyfire-pay-id").strip()


def inbound_expected_audience() -> str | None:
    val = (os.environ.get("KYA_EXPECTED_AUDIENCE") or "").strip()
    return val or None


def inbound_expected_environment() -> str | None:
    val = (os.environ.get("KYA_EXPECTED_ENVIRONMENT") or "").strip()
    return val or None


def evaluate_inbound(
    *,
    token: str | None,
    policy: str | None = None,
    expected_audience: str | None = None,
    expected_environment: str | None = None,
    trusted_issuers: set[str] | None = None,
    clock_skew: int = DEFAULT_CLOCK_SKEW,
    enforce_replay_protection: bool = True,
) -> dict[str, Any]:
    """Apply the configured KYA inbound policy to an incoming request token.

    Returns ``{"action", "report", "reason"}`` where ``action`` is one of:
      - ``"skip"`` — policy is off, or token absent under opportunistic policy
      - ``"accept"`` — token present and verified
      - ``"warn"``   — token present but invalid, under opportunistic policy
      - ``"reject"`` — token absent/invalid under required policy

    The caller is responsible for converting ``"reject"`` into an HTTP 401/403.
    """
    effective_policy = policy or inbound_policy()
    if effective_policy == "off":
        return {"action": "skip", "report": None, "reason": "policy_off"}

    if not token:
        if effective_policy == "required":
            return {
                "action": "reject",
                "report": None,
                "reason": "kya_token_required_but_missing",
            }
        return {"action": "skip", "report": None, "reason": "no_token"}

    try:
        report = verify_token(
            token_type="kya",  # we accept kya+ and kya-pay+ through this lane
            token=token,
            expected_audience=expected_audience or inbound_expected_audience(),
            expected_environment=expected_environment or inbound_expected_environment(),
            trusted_issuers=trusted_issuers if trusted_issuers is not None else parse_trusted_issuers(None),
            clock_skew=clock_skew,
            enforce_audience=True,
            enforce_replay_protection=enforce_replay_protection,
        )
    except ValueError as err:
        # Malformed JWT (bad base64, not enough segments, non-JSON header/payload).
        # Treat as a verification failure rather than crashing the request path.
        report = ValidationReport(token_type="kya")
        report.errors.append(f"Malformed token: {err}")
        report.ok = False
        if effective_policy == "required":
            return {
                "action": "reject",
                "report": report,
                "reason": "kya_token_malformed",
            }
        return {"action": "warn", "report": report, "reason": "kya_token_malformed"}

    # Allow kya-pay tokens through the same header (their typ differs).
    if not report.signature_verified or report.errors:
        # Retry as kya-pay if the only complaint is typ mismatch.
        if any("typ='kya-pay+jwt'" in e or "kya-pay" in e for e in report.errors):
            report = verify_token(
                token_type="kya-pay",
                token=token,
                expected_audience=expected_audience or inbound_expected_audience(),
                expected_environment=expected_environment or inbound_expected_environment(),
                trusted_issuers=trusted_issuers if trusted_issuers is not None else parse_trusted_issuers(None),
                clock_skew=clock_skew,
                enforce_audience=True,
                enforce_replay_protection=enforce_replay_protection,
            )

    if report.ok:
        return {"action": "accept", "report": report, "reason": None}
    if effective_policy == "required":
        return {
            "action": "reject",
            "report": report,
            "reason": "kya_verification_failed",
        }
    return {"action": "warn", "report": report, "reason": "kya_verification_failed"}
