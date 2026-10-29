/**
 * HMAC signature check for incoming webhook calls.
 *
 * The caller signs the raw request body with the trigger's shared secret and
 * sends `X-DevServer-Signature: sha256=<hex digest>` — the same scheme GitHub
 * uses, so most webhook senders can be configured without custom code.
 */

import { createHmac, timingSafeEqual } from 'node:crypto';

const SIGNATURE_PREFIX = 'sha256=';
const HEX_DIGEST = /^[0-9a-f]{64}$/i;

/** The header value a sender should produce for `rawBody`. */
export function signBody(rawBody: string | Buffer, secret: string): string {
  return SIGNATURE_PREFIX + createHmac('sha256', secret).update(rawBody).digest('hex');
}

/**
 * True when `header` is a valid signature of `rawBody` under `secret`.
 * Never throws: a missing, malformed or wrong-length header is simply false.
 */
export function verifySignature(
  rawBody: string | Buffer,
  header: string | null,
  secret: string,
): boolean {
  if (!header || !secret) return false;
  if (!header.startsWith(SIGNATURE_PREFIX)) return false;

  const providedHex = header.slice(SIGNATURE_PREFIX.length).trim();
  if (!HEX_DIGEST.test(providedHex)) return false;

  const expected = createHmac('sha256', secret).update(rawBody).digest();
  const provided = Buffer.from(providedHex, 'hex');
  // timingSafeEqual throws on unequal lengths; the regex above already pins
  // the length, but the explicit check keeps this function throw-free.
  if (provided.length !== expected.length) return false;
  return timingSafeEqual(provided, expected);
}
