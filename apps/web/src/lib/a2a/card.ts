/**
 * Agent Card construction.
 *
 * The card is how an external agent discovers what this DevServer can do. It is
 * served at ``/.well-known/agent-card.json`` (RFC 8615) via a rewrite in
 * ``next.config.ts`` to ``/api/a2a/card``, and the
 * authenticated variant (``agent/getExtendedAgentCard``) additionally names the
 * specific repos the calling peer is scoped to.
 *
 * The public card advertises *capability*, never inventory: it must not leak
 * repo names, task counts, or anything else about this deployment, because on a
 * private-network deployment the card is still the least-protected surface.
 */

import { query } from '@/lib/db';
import type { A2AAgentCard, A2ASkill } from './protocol';
import { A2A_PROTOCOL_VERSION } from './protocol';
import type { A2APeer } from './auth';

/**
 * Skills, keyed by DevServer task type.
 *
 * The task-type list mirrors ``routes/internal.py``'s ``/internal/agent-registry``
 * and the web registry ``lib/task-types.ts``. Kept as a literal here rather than
 * fetched from the worker so the card is servable when the worker is down —
 * discovery should not depend on the execution tier being up.
 */
const SKILLS: Record<string, A2ASkill> = {
  coding: {
    id: 'coding',
    name: 'Implement a code change',
    description:
      'Implement a change in a git repository: write the code, run the verifier, ' +
      'and produce a pull request or a git format-patch series. Runs in an isolated ' +
      'worktree with automatic retries and error classification.',
    tags: ['code', 'software-engineering', 'git', 'pull-request'],
    examples: [
      'Add rate limiting to the /api/search endpoint and cover it with tests.',
      'Upgrade the project to React 19 and fix the resulting type errors.',
    ],
    inputModes: ['text/plain'],
    outputModes: ['text/plain', 'application/mbox'],
  },
  test: {
    id: 'test',
    name: 'Run and fix tests',
    description:
      'Run a repository test suite, diagnose failures, and fix them. The agent runs ' +
      'the tests itself and reports results.',
    tags: ['code', 'testing', 'ci'],
    examples: ['Run the integration suite and fix whatever is failing on main.'],
    inputModes: ['text/plain'],
    outputModes: ['text/plain'],
  },
  script: {
    id: 'script',
    name: 'Run a script or automation',
    description:
      'Execute a one-off script or maintenance automation against a repository and ' +
      'report its output.',
    tags: ['automation', 'scripting'],
    inputModes: ['text/plain'],
    outputModes: ['text/plain'],
  },
  skill: {
    id: 'skill',
    name: 'Run a packaged skill',
    description:
      'Run a packaged DevServer skill in a scratch workspace. No repository changes ' +
      'are made and no pull request is opened.',
    tags: ['automation', 'skill'],
    inputModes: ['text/plain'],
    outputModes: ['text/plain', 'text/markdown'],
  },
  research: {
    id: 'research',
    name: 'Research a codebase question',
    description:
      'Answer a question about a codebase or a technical topic. Read-only: the ' +
      'description is the prompt and the answer is returned as the artifact.',
    tags: ['research', 'question-answering', 'code-comprehension'],
    examples: [
      'How does authentication flow through this service? Cite the files.',
      'What would break if we removed the legacy queue adapter?',
    ],
    inputModes: ['text/plain'],
    outputModes: ['text/plain', 'text/markdown'],
  },
};

const DESCRIPTION =
  'DevServer is an autonomous software-engineering agent. Delegate a coding, ' +
  'testing, or research task and it runs a coding-agent CLI in an isolated git ' +
  'worktree, verifies the result, screens the diff for secrets, and returns a ' +
  'pull request or patch series. Long-running tasks report progress and can pause ' +
  'to ask the caller for a decision. Tasks run under operator-set governance: ' +
  'by default a human plan-approval gate, a daily task quota and, when ' +
  'configured, a wall-clock ceiling.';
// Deliberately no "spend ceiling" claim: A2A tasks run in subscription (`max`)
// billing mode, where the CLI reports cost_usd=0 and `max_cost_usd` is not
// enforced (see services/budget.py). Only `max_wall_seconds` is enforced.

async function agentVersion(): Promise<string> {
  return process.env.NEXT_PUBLIC_VERSION || '0.0.0';
}

/**
 * The public, unauthenticated card.
 *
 * ``skills`` lists every task type this deployment can run. What a *particular*
 * peer is allowed to invoke is a separate question, answered by the extended
 * card — advertising a skill is not granting access to it.
 */
export async function buildPublicCard(origin: string): Promise<A2AAgentCard> {
  return {
    name: 'DevServer',
    description: DESCRIPTION,
    version: await agentVersion(),
    protocolVersion: A2A_PROTOCOL_VERSION,
    url: `${origin}/api/a2a`,
    preferredTransport: 'JSONRPC',
    provider: { organization: 'DevServer' },
    capabilities: {
      // SSE via message/stream + tasks/subscribe.
      streaming: true,
      // Per-task webhooks via tasks/pushNotificationConfig/*.
      pushNotifications: true,
      // No stored per-state history yet — a client must not ask for it.
      stateTransitionHistory: false,
    },
    defaultInputModes: ['text/plain'],
    defaultOutputModes: ['text/plain', 'text/markdown'],
    securitySchemes: {
      bearer: {
        type: 'http',
        scheme: 'bearer',
        description:
          'A per-peer token issued by the DevServer operator. Contact the operator ' +
          'to be provisioned; tokens cannot be self-registered.',
      },
    },
    security: [{ bearer: [] }],
    skills: Object.values(SKILLS),
  };
}

/**
 * The authenticated card: the public card narrowed to what this peer may
 * actually do, plus one skill per repo it is scoped to.
 */
export async function buildExtendedCard(
  origin: string,
  peer: A2APeer,
): Promise<A2AAgentCard> {
  const card = await buildPublicCard(origin);

  const allowedTypes = peer.allowed_task_types || [];
  const skills: A2ASkill[] = allowedTypes
    .map((t) => SKILLS[t])
    .filter((s): s is A2ASkill => !!s);

  const repoIds = peer.allowed_repo_ids || [];
  if (repoIds.length > 0) {
    const res = await query<{ id: number; name: string; description: string | null }>(
      `SELECT id, name, description FROM repos WHERE id = ANY($1::int[]) ORDER BY name`,
      [repoIds],
    );
    for (const repo of res.rows) {
      skills.push({
        id: `repo-${repo.id}`,
        name: `Work on ${repo.name}`,
        description:
          repo.description ||
          `Delegate tasks against the ${repo.name} repository.`,
        tags: ['repository', repo.name],
        inputModes: ['text/plain'],
        outputModes: ['text/plain'],
      });
    }
  }

  return {
    ...card,
    name: `DevServer (${peer.name})`,
    skills,
  };
}

export { SKILLS };
