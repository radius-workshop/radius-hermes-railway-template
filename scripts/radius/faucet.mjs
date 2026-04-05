import { FAUCET_BASE } from './radius-chain.mjs';

async function getChallenge(address) {
  const res = await fetch(`${FAUCET_BASE}/challenge/${address}?token=SBC`);
  if (!res.ok) throw new Error(`Challenge request failed: ${res.status}`);
  const data = await res.json();
  return data.message || data.challenge;
}

export async function requestFaucetDrip({ signer, token = 'SBC' }) {
  const address = await signer.getAddress();

  const unsignedRes = await fetch(`${FAUCET_BASE}/drip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address, token }),
  });

  const unsignedData = await unsignedRes.json().catch(() => ({}));
  if (unsignedRes.ok) {
    return { ok: true, address, txHash: unsignedData.tx_hash || unsignedData.txHash || unsignedData.hash };
  }

  const errCode = unsignedData.error || '';
  if (errCode !== 'signature_required' && unsignedRes.status !== 401) {
    if (errCode === 'rate_limited') {
      return { ok: false, address, message: 'Faucet rate limited.' };
    }
    return { ok: false, address, message: unsignedData.error || unsignedData.message || `Faucet failed (${unsignedRes.status})` };
  }

  const message = await getChallenge(address);
  const signature = await signer.signMessage(message);

  const signedRes = await fetch(`${FAUCET_BASE}/drip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address, token, signature }),
  });

  const signedData = await signedRes.json().catch(() => ({}));
  if (!signedRes.ok) {
    return { ok: false, address, message: signedData.error || signedData.message || `Faucet failed (${signedRes.status})` };
  }

  return { ok: true, address, txHash: signedData.tx_hash || signedData.txHash || signedData.hash };
}
