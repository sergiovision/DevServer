/**
 * Patch file names as they may appear in a download URL.
 *
 * The worker validates names too, but the web route checks first so an
 * obviously hostile name (`../../etc/passwd`, `x.sh`) is rejected before any
 * request leaves this process.
 */
const PATCH_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*\.patch$/;

export function isValidPatchFileName(name: string): boolean {
  return PATCH_FILE_NAME.test(name);
}
