"""JWT / DID authentication for the agent server."""
import os
import time
import base64
import json
import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from logging_utils import log_event

logger = logging.getLogger(__name__)

# ——— Module-level state ———
_private_key = None   # cryptography EllipticCurvePrivateKey
_did: Optional[str] = None
_did_document: Optional[dict] = None

# ——— DID helpers ———

def url_to_did_web(base_url: str) -> str:
    """Convert an HTTP(S) URL to a did:web DID."""
    parsed = urlparse(base_url)
    host = parsed.netloc.replace(":", "%3A")
    path = parsed.path.strip("/")
    if path:
        return f"did:web:{host}:{':'.join(path.split('/'))}"
    return f"did:web:{host}"


def did_web_to_url(did: str) -> str:
    """Convert a did:web DID to its document fetch URL."""
    # Strip DID fragment (e.g. #controller) — it identifies a key within the
    # document and must never appear in the fetch URL.
    did = did.split("#")[0]
    did_path = did[8:]  # strip "did:web:"
    parts = did_path.split(":")
    host = parts[0].replace("%3A", ":")
    if len(parts) == 1:
        return f"https://{host}/.well-known/did.json"
    return f"https://{host}/{'/'.join(parts[1:])}/did.json"


def private_key_from_hex(hex_key: str):
    """Load a secp256k1 private key from a hex string."""
    from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key, SECP256K1
    from cryptography.hazmat.backends import default_backend
    return derive_private_key(int(hex_key.lstrip("0x"), 16), SECP256K1(), default_backend())


def public_key_to_jwk(public_key) -> dict:
    """Serialize a secp256k1 public key to JWK format."""
    nums = public_key.public_numbers()
    return {
        "kty": "EC",
        "crv": "secp256k1",
        "x": base64.urlsafe_b64encode(nums.x.to_bytes(32, "big")).rstrip(b"=").decode(),
        "y": base64.urlsafe_b64encode(nums.y.to_bytes(32, "big")).rstrip(b"=").decode(),
    }


def jwk_to_public_key(jwk: dict):
    """Load a secp256k1 public key from a JWK dict."""
    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicNumbers, SECP256K1
    from cryptography.hazmat.backends import default_backend

    def b64url_int(s: str) -> int:
        return int.from_bytes(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), "big")

    return EllipticCurvePublicNumbers(b64url_int(jwk["x"]), b64url_int(jwk["y"]), SECP256K1()).public_key(
        default_backend()
    )


def create_did_web_document(private_key, base_url: str) -> tuple:
    """Create a did:web DID and W3C DID document from a private key."""
    did = url_to_did_web(base_url)
    jwk = public_key_to_jwk(private_key.public_key())
    vm_id = f"{did}#controller"
    doc = {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/suites/jws-2020/v1",
        ],
        "id": did,
        "verificationMethod": [
            {
                "id": vm_id,
                "type": "JsonWebKey2020",
                "controller": did,
                "publicKeyJwk": {**jwk, "alg": "ES256K"},
            }
        ],
        "authentication": [vm_id],
        "assertionMethod": [vm_id],
    }
    return did, doc


# ——— DID resolution cache ———
_did_cache: dict = {}
_CACHE_TTL = 300  # seconds


async def resolve_did_to_public_key(did: str):
    """Resolve a DID to a cryptography public key (async, cached)."""
    now = time.time()

    # Fast path: our own DID uses the in-memory document (no HTTP).
    if did == _did and _did_document is not None:
        return _extract_pub_key(_did_document)

    if did in _did_cache:
        pub_key, expires_at = _did_cache[did]
        if now < expires_at:
            return pub_key

    if did.startswith("did:web:"):
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(did_web_to_url(did))
            resp.raise_for_status()
            doc = resp.json()
    elif did.startswith("did:key:"):
        doc = _resolve_did_key_doc(did)
    else:
        raise ValueError(f"Unsupported DID method: {did}")

    pub_key = _extract_pub_key(doc)
    _did_cache[did] = (pub_key, now + _CACHE_TTL)
    return pub_key


def _resolve_did_key_doc(did: str) -> dict:
    """Build a synthetic DID document for a did:key DID."""
    key_id = did[8:]
    if not key_id.startswith("z"):
        raise ValueError(f"Unsupported multibase prefix in {did}")

    raw = _b58decode(key_id[1:])
    codec, codec_len = _read_varint(raw)
    key_bytes = raw[codec_len:]

    SECP256K1_CODEC = 0xE7
    if codec != SECP256K1_CODEC:
        raise ValueError(f"Unsupported key codec {hex(codec)} in {did}")

    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey, SECP256K1
    pub_key = EllipticCurvePublicKey.from_encoded_point(SECP256K1(), key_bytes)
    jwk = public_key_to_jwk(pub_key)
    vm_id = f"{did}#{key_id}"
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": did,
        "verificationMethod": [
            {"id": vm_id, "type": "JsonWebKey2020", "controller": did, "publicKeyJwk": {**jwk, "alg": "ES256K"}}
        ],
        "authentication": [vm_id],
    }


def _extract_pub_key(doc: dict, kid: Optional[str] = None):
    vms = doc.get("verificationMethod", [])
    if not vms:
        raise ValueError("DID document has no verificationMethod")
    vm = next((v for v in vms if kid and v.get("id") == kid), vms[0])
    jwk = vm.get("publicKeyJwk")
    if not jwk:
        raise ValueError("No publicKeyJwk in verification method")
    return jwk_to_public_key(jwk)


# ——— Base58 / varint helpers ———

_B58_ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_ALPHA.index(c)
    result = []
    while n:
        result.append(n & 0xFF)
        n >>= 8
    pad = sum(1 for c in s if c == "1")
    return b"\x00" * pad + bytes(reversed(result))


def _read_varint(data: bytes) -> tuple:
    n, shift = 0, 0
    for i, byte in enumerate(data):
        n |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return n, i + 1
        shift += 7
    raise ValueError("Invalid varint")


# ——— JWT operations ———

def _create_jwt(sub: str, issuer: str, private_key, expires_in: int = 86400) -> str:
    import jwt
    # iss must be the bare DID — strip any fragment (e.g. #controller) that
    # may have leaked in from the verificationMethod id.
    bare_issuer = issuer.split("#")[0]
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "iss": bare_issuer, "iat": now, "exp": now + expires_in},
        private_key,
        algorithm="ES256K",
        headers={"kid": f"{bare_issuer}#controller"},
    )


async def _verify_jwt(token: str) -> dict:
    import jwt as pyjwt

    try:
        unverified = pyjwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        raise ValueError(f"Malformed JWT: {e}")

    issuer = unverified.get("iss")
    if not issuer:
        raise ValueError("JWT missing iss claim")
    # Normalize: strip DID fragment if present (e.g. "did:web:host#controller" → "did:web:host")
    issuer = issuer.split("#")[0]

    header = pyjwt.get_unverified_header(token)
    kid = header.get("kid")

    # Resolve issuer DID → public key
    if issuer == _did and _did_document is not None:
        pub_key = _extract_pub_key(_did_document, kid)
    else:
        pub_key = await resolve_did_to_public_key(issuer)

    try:
        verified = pyjwt.decode(token, pub_key, algorithms=["ES256K"])
    except pyjwt.ExpiredSignatureError:
        raise ValueError("JWT has expired")
    except Exception as e:
        raise ValueError(f"JWT verification failed: {e}")

    return {"issuer": issuer, "payload": verified}


# ——— Public API ———

async def setup_auth(base_url: str) -> str:
    """
    Initialize JWT auth from RADIUS_PRIVATE_KEY (or generate an ephemeral key).
    Must be called before accepting requests. Returns this agent's DID.
    """
    global _private_key, _did, _did_document

    raw_key = os.environ.get("RADIUS_PRIVATE_KEY", "")
    if raw_key:
        _private_key = private_key_from_hex(raw_key)
    else:
        from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256K1
        from cryptography.hazmat.backends import default_backend
        _private_key = generate_private_key(SECP256K1(), default_backend())
        log_event(
            logger,
            logging.WARNING,
            "Using ephemeral auth keypair",
            event="auth.setup",
            auth_mode="ephemeral",
            warning_code="radius_private_key_missing",
        )

    _did, _did_document = create_did_web_document(_private_key, base_url)
    log_event(
        logger,
        logging.INFO,
        "Auth initialized",
        event="auth.setup",
        auth_mode="persistent" if raw_key else "ephemeral",
        issuer_did=_did,
        base_url=base_url,
    )
    return _did


def get_did() -> Optional[str]:
    return _did


def get_did_document() -> Optional[dict]:
    return _did_document


async def issue_token(sub: str, expires_in: int = 86400) -> str:
    """Issue a signed JWT for the given subject."""
    if _private_key is None:
        raise RuntimeError("[auth] Not initialized — call setup_auth() first")
    return _create_jwt(sub, _did, _private_key, expires_in)


# ——— FastAPI dependency ———

async def jwt_auth_dep(request: Request) -> dict:
    """FastAPI dependency: validates Bearer JWT and returns {issuer, payload}."""
    if _private_key is None:
        raise HTTPException(status_code=503, detail="Service unavailable")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Bearer realm="agent-server"'},
        )

    token = auth[7:].strip()
    if len(token.split(".")) != 3:
        raise HTTPException(status_code=401, detail="Invalid token format")

    try:
        result = await _verify_jwt(token)
    except ValueError as e:
        log_event(
            logger,
            logging.ERROR,
            "JWT verification failed",
            event="auth.jwt_rejected",
            auth_error=str(e),
        )
        raise HTTPException(status_code=403, detail="Forbidden")

    issuer = result["issuer"]
    trusted_env = os.environ.get("TRUSTED_DIDS", "")
    if trusted_env:
        allowed = {_did} | {d.strip() for d in trusted_env.split(",") if d.strip()}
        if issuer not in allowed:
            log_event(
                logger,
                logging.ERROR,
                "Issuer DID not trusted",
                event="auth.jwt_rejected",
                auth_error="issuer_not_trusted",
                issuer_did=issuer,
                trusted_did_count=len(allowed),
            )
            raise HTTPException(status_code=403, detail="Forbidden")

    return result
