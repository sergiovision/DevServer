import { NextRequest, NextResponse } from 'next/server';
import { apiErrorResponse } from '@/lib/api-errors';
import { parseTriggerCreate } from '@/lib/webhooks/trigger-input';
import { createTrigger, listTriggers } from '@/lib/webhooks/triggers';

/** GET /api/webhook-triggers — every trigger, secrets replaced by `has_secret`. */
export async function GET() {
  try {
    return NextResponse.json(await listTriggers());
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/webhook-triggers');
  }
}

/**
 * POST /api/webhook-triggers
 *
 * Body: { name, repo_id, secret, template_id?, title_template?,
 *         description_template?, task_key_prefix?, priority?, enabled? }
 * The secret is mandatory: an unsigned webhook would let anyone who learns
 * the URL queue work on your repositories.
 */
export async function POST(request: NextRequest) {
  const body: unknown = await request.json().catch(() => null);
  const input = parseTriggerCreate(body);
  if (!input.ok) {
    return NextResponse.json({ error: input.error }, { status: 400 });
  }

  try {
    return NextResponse.json(await createTrigger(input.values), { status: 201 });
  } catch (err) {
    return apiErrorResponse(err, 'POST /api/webhook-triggers');
  }
}
