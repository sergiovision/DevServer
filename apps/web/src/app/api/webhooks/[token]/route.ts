import { createWebhookHandler } from '@/lib/webhooks/handler';
import { postgresWebhookStore } from '@/lib/webhooks/store';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * POST /api/webhooks/<token>
 *
 * Public entry point for external systems (CI, issue trackers, chat bots).
 * The call must carry `X-DevServer-Signature: sha256=<HMAC of the raw body>`;
 * on success a task is created and queued: 201 `{ taskId, taskKey }`.
 */
export const POST = createWebhookHandler(postgresWebhookStore);
