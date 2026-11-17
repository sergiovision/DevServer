/**
 * Egress secret redaction for the A2A gateway.
 *
 * A faithful TypeScript port of ``apps/worker/src/services/secret_rules.py``
 * (used there by ``pr_preflight`` and the log redactor). The A2A gateway lives
 * in the Next.js tier, so it cannot call the Python helper without a worker
 * round-trip on every artifact read — and the worker has no auth, so adding a
 * hop there is the wrong direction.
 *
 * KEEP IN SYNC with the Python rules. If you add a pattern there, add it here.
 *
 * Everything DevServer hands back to an external peer — task log tails, agent
 * output, error text — passes through ``redactSecrets``. Task output is agent
 * stdout from a repo full of credentials; assuming it is clean is not an option.
 */

interface SecretRule {
  key: string;
  pattern: RegExp;
  /** Redact only capture group 1, preserving surrounding context. */
  captureOnly?: boolean;
}

// All patterns carry `g` so every occurrence is replaced, matching Python's
// `pattern.sub()` (which is replace-all).
const SECRET_RULES: SecretRule[] = [
  { key: 'anthropic_api_key', pattern: /sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}/g },
  // Negative lookahead so Anthropic keys (also `sk-`) don't match twice.
  { key: 'openai_api_key', pattern: /\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{32,}/g },
  { key: 'aws_access_key', pattern: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g },
  { key: 'github_pat', pattern: /\bghp_[A-Za-z0-9]{36}\b/g },
  { key: 'github_fine_grained', pattern: /\bgithub_pat_[A-Za-z0-9_]{82}\b/g },
  { key: 'github_token', pattern: /\b(?:gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36}\b/g },
  // Python redacts the WHOLE match for this rule (only hardcoded_password is
  // capture-only there), so the `gitea_token=` label is replaced too.
  {
    key: 'gitea_token',
    pattern: /(?:gitea[_-]?token|GITEA_TOKEN)["'\s]*[:=]["'\s]*([a-f0-9]{40})/gi,
  },
  { key: 'slack_token', pattern: /\bxox[baprs]-[A-Za-z0-9-]{10,48}\b/g },
  { key: 'google_api_key', pattern: /\bAIza[0-9A-Za-z_-]{35}\b/g },
  { key: 'stripe_secret', pattern: /\bsk_live_[A-Za-z0-9]{24,}\b/g },
  {
    key: 'private_key',
    pattern: /-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----/g,
  },
  { key: 'telegram_bot_token', pattern: /\b\d{9,11}:AA[A-Za-z0-9_-]{33}\b/g },
  {
    key: 'hardcoded_password',
    pattern: /(?:password|passwd|pwd)\s*[:=]\s*['"]([^'"\s]{8,})['"]/gi,
    captureOnly: true,
  },
];

// Placeholders that should not be flagged even when they match a password rule.
// Mirrors `_PASSWORD_EXACT_PLACEHOLDERS` / `_PASSWORD_SUBSTRING_PLACEHOLDERS` in
// secret_rules.py verbatim (Python is the authority): the exact set DOES
// contain "password" and "secret" — only a value that is exactly one of these
// words is skipped; a real password merely containing them is still redacted.
const PASSWORD_EXACT_PLACEHOLDERS = new Set([
  'changeme', 'password', 'secret', 'your_password', 'yourpassword',
  'placeholder', 'example', 'none', 'todo', 'fixme',
  'your-password-here', '<password>', '<your-password>',
]);
const PASSWORD_SUBSTRING_PLACEHOLDERS = ['xxxxxx', '######', '******'];

function isPlaceholder(value: string): boolean {
  const v = value.toLowerCase();
  if (PASSWORD_EXACT_PLACEHOLDERS.has(v)) return true;
  return PASSWORD_SUBSTRING_PLACEHOLDERS.some((p) => v.includes(p));
}

/**
 * Replace every secret match with ``[REDACTED:<rule_key>]``.
 *
 * Returns the redacted text plus the distinct rule keys that fired, so the
 * caller can surface "we redacted something" without echoing the secret.
 */
export function redactSecrets(content: string): { text: string; found: string[] } {
  if (!content) return { text: content ?? '', found: [] };

  let text = content;
  const found = new Set<string>();

  for (const rule of SECRET_RULES) {
    // Fresh RegExp per pass: `g` regexes carry mutable lastIndex, and reusing
    // the module-level literal across calls would make matching order-dependent.
    const re = new RegExp(rule.pattern.source, rule.pattern.flags);
    text = text.replace(re, (match, group1: string | undefined) => {
      if (rule.captureOnly) {
        if (!group1) return match;
        if (rule.key === 'hardcoded_password' && isPlaceholder(group1)) return match;
        found.add(rule.key);
        return match.replace(group1, `[REDACTED:${rule.key}]`);
      }
      found.add(rule.key);
      return `[REDACTED:${rule.key}]`;
    });
  }

  return { text, found: [...found] };
}

/**
 * Strip absolute host filesystem paths.
 *
 * Separate from secret redaction because it is an information-disclosure
 * concern rather than a credential one: ``PatchSet.to_dict()["directory"]`` and
 * agent stdout both leak the operator's real directory layout (and often their
 * username) to a caller who has no business knowing it.
 */
export function stripHostPaths(content: string): string {
  if (!content) return content ?? '';
  return content
    .replace(/\/(?:Users|home)\/[^/\s"']+/g, '~')
    .replace(/\/(?:opt|srv|var\/lib)\/[A-Za-z0-9._-]+\/[^\s"']*/g, '<path>');
}

/** The full egress filter applied to every text Part leaving the gateway. */
export function sanitizeForPeer(content: string | null | undefined): string {
  if (!content) return '';
  return stripHostPaths(redactSecrets(content).text);
}

/**
 * Apply `sanitizeForPeer` to every string (values and keys) inside a JSON
 * value — for structured payloads such as `task_runs.plan_json` that leave the
 * gateway as a data Part. Depth-capped so a pathological document cannot
 * recurse without bound.
 */
export function sanitizeJsonForPeer(value: unknown, depth = 0): unknown {
  if (typeof value === 'string') return sanitizeForPeer(value);
  if (value === null || typeof value !== 'object') return value;
  if (depth > 32) return null;
  if (Array.isArray(value)) return value.map((v) => sanitizeJsonForPeer(v, depth + 1));
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    out[sanitizeForPeer(k)] = sanitizeJsonForPeer(v, depth + 1);
  }
  return out;
}
