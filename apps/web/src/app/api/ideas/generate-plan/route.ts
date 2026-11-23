import { NextRequest } from 'next/server';
import { jsonError, proxyWorker } from '@/lib/route-utils';

/**
 * POST /api/ideas/generate-plan   body: { project_name: string, description: string }
 *
 * DevPlan: the worker's devplan skill turns an idea into an implementation
 * plan through the system LLM (optionally mirrored to OBSIDIAN_FOLDER). A pure
 * relay of /internal/generate-plan; the worker's error detail is forwarded.
 */

// A full plan is a long LLM generation, possibly through a vendor CLI.
const GENERATE_TIMEOUT_MS = 600_000;

export async function POST(req: NextRequest) {
  const payload = (await req.json().catch(() => null)) as {
    project_name?: unknown;
    description?: unknown;
  } | null;
  const projectName = payload?.project_name;
  const description = payload?.description;
  if (
    typeof projectName !== 'string' ||
    !projectName.trim() ||
    typeof description !== 'string' ||
    !description.trim()
  ) {
    return jsonError(400, 'project_name and description are required');
  }

  return proxyWorker('/internal/generate-plan', {
    method: 'POST',
    body: { project_name: projectName, description },
    timeoutMs: GENERATE_TIMEOUT_MS,
  });
}
