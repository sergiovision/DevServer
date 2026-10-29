import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { signBody, verifySignature } from './signature';

describe('verifySignature', () => {
  const secret = 'shared-secret';
  const body = '{"hello":"world"}';

  it('accepts a correct signature', () => {
    assert.equal(verifySignature(body, signBody(body, secret), secret), true);
  });

  it('accepts a Buffer body', () => {
    assert.equal(verifySignature(Buffer.from(body), signBody(body, secret), secret), true);
  });

  it('rejects a signature made with another secret', () => {
    assert.equal(verifySignature(body, signBody(body, 'other-secret'), secret), false);
  });

  it('rejects a signature of a different body', () => {
    assert.equal(verifySignature('{"hello":"there"}', signBody(body, secret), secret), false);
  });

  it('rejects missing, malformed and wrong-length headers without throwing', () => {
    assert.equal(verifySignature(body, null, secret), false);
    assert.equal(verifySignature(body, '', secret), false);
    assert.equal(verifySignature(body, 'sha1=abcdef', secret), false);
    assert.equal(verifySignature(body, 'sha256=abcdef', secret), false);
    assert.equal(verifySignature(body, `sha256=${'z'.repeat(64)}`, secret), false);
  });

  it('rejects everything when the secret is empty', () => {
    assert.equal(verifySignature(body, signBody(body, ''), ''), false);
  });
});
