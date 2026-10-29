/**
 * HTTP adapter for webhook firing: Request in, Response out.
 *
 * Built from a `WebhookStore` so the route file only wires in the Postgres
 * store, while tests drive the very same handler with an in-memory fake.
 * Uses the standard `Response` API, so it does not depend on Next.js.
 */

import { fireWebhook, SIGNATURE_HEADER, type FireOptions, type WebhookStore } from './fire';

export interface WebhookRouteContext {
  params: Promise<{ token: string }>;
}

export function createWebhookHandler(store: WebhookStore, options: FireOptions = {}) {
  return async function handleWebhook(
    request: Request,
    context: WebhookRouteContext,
  ): Promise<Response> {
    const { token } = await context.params;
    // The raw text, not request.json(): the signature covers the exact bytes sent.
    const rawBody = await request.text();

    try {
      const outcome = await fireWebhook(
        store,
        { token, rawBody, signature: request.headers.get(SIGNATURE_HEADER) },
        options,
      );
      if (!outcome.ok) {
        return Response.json({ error: outcome.error }, { status: outcome.status });
      }
      return Response.json({ taskId: outcome.taskId, taskKey: outcome.taskKey }, { status: 201 });
    } catch (err) {
      console.error(`POST /api/webhooks/<token>:`, err);
      return Response.json({ error: 'Internal server error' }, { status: 500 });
    }
  };
}
