// auth.ts — JWT gate using ACK (agentcommercekit)
import {
  generateKeypair,
  createJwtSigner,
  createJwt,
  verifyJwt,
  isJwtString,
  hexStringToBytes,
  createDidWebDocumentFromKeypair,
  getDidResolver,
} from "agentcommercekit"
import type { MiddlewareHandler } from "hono"

interface AuthState {
  did: string
  didDocument: Record<string, unknown>
  signer: ReturnType<typeof createJwtSigner>
}

const resolver = getDidResolver()
let state: AuthState | null = null

/** Returns this agent's did:web DID after setupAuth() has been called. */
export function getDid(): string | null {
  return state?.did ?? null
}

/** Returns the W3C DID document after setupAuth() has been called. */
export function getDidDocument(): Record<string, unknown> | null {
  return state?.didDocument ?? null
}

/**
 * Initialize JWT auth. Call once at startup before accepting requests.
 * Derives the signing keypair from RADIUS_PRIVATE_KEY (shared with the wallet).
 * Constructs a did:web DID from baseUrl, resolvable via /.well-known/did.json.
 * Returns the agent's DID.
 */
export async function setupAuth(baseUrl: string): Promise<string> {
  const rawKey = process.env.RADIUS_PRIVATE_KEY

  let privateKeyBytes: Uint8Array | undefined
  if (rawKey) {
    privateKeyBytes = hexStringToBytes(rawKey)
  }

  const keypair = await generateKeypair("secp256k1", privateKeyBytes)

  if (!rawKey) {
    console.warn(
      "[auth] RADIUS_PRIVATE_KEY not set — using ephemeral keypair. " +
        "Tokens will not survive restarts."
    )
  }

  const { did, didDocument } = createDidWebDocumentFromKeypair({ keypair, baseUrl })

  state = {
    did,
    didDocument: didDocument as unknown as Record<string, unknown>,
    signer: createJwtSigner(keypair),
  }

  console.log(`[auth] JWT issuer DID: ${state.did}`)
  return state.did
}

/**
 * Issue a signed JWT. `sub` identifies the token holder.
 * Tokens expire after `expiresInSeconds` (default 24 h).
 */
export async function issueToken(
  sub: string,
  expiresInSeconds = 86_400
): Promise<string> {
  if (!state) throw new Error("[auth] Not initialized — call setupAuth() first")

  const now = Math.floor(Date.now() / 1000)
  return createJwt(
    { sub, iat: now, exp: now + expiresInSeconds },
    { issuer: state.did, signer: state.signer },
    { alg: "ES256K" }
  )
}

/**
 * Hono middleware — accepts any cryptographically valid DID JWT.
 * If TRUSTED_DIDS is set (comma-separated), only those DIDs are allowed
 * in addition to this agent's own DID (so self-issued /token tokens always work).
 * Returns 401 if the Authorization header is missing, 403 if the token is invalid.
 */
export const jwtAuth: MiddlewareHandler = async (c, next) => {
  if (!state) return c.json({ error: "Service unavailable" }, 503)

  const raw = c.req.header("Authorization")
  if (!raw?.startsWith("Bearer ")) {
    c.header("WWW-Authenticate", 'Bearer realm="agent-server"')
    return c.json({ error: "Unauthorized" }, 401)
  }

  const token = raw.slice(7).trim()
  if (!isJwtString(token)) {
    return c.json({ error: "Invalid token format" }, 401)
  }

  try {
    const verified = await verifyJwt(token, { resolver })

    const trustedEnv = process.env.TRUSTED_DIDS
    if (trustedEnv) {
      const allowed = new Set([
        state.did, // self-issued tokens always accepted
        ...trustedEnv.split(",").map((d) => d.trim()).filter(Boolean),
      ])
      if (!allowed.has(verified.issuer)) {
        return c.json({ error: "Forbidden" }, 403)
      }
    }

    await next()
  } catch {
    return c.json({ error: "Forbidden" }, 403)
  }
}
