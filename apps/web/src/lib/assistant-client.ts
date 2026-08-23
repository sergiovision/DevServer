/**
 * Browser-side client for the Ask Agent panel.
 *
 * The endpoint is POST-with-an-SSE-body rather than a GET `EventSource`,
 * because `EventSource` cannot POST and a turn carries a message, a transcript
 * and page context. So the stream is consumed with `fetch` +
 * `body.getReader()` and framed here.
 *
 * The event vocabulary is produced identically by both worker engines — the
 * HTTP system-LLM path and the Pro agent-CLI path — so the panel renders one
 * shape regardless of which answered.
 */

export type AssistantEvent =
  | { type: 'session'; session_id: string; engine: 'chat' | 'agent'; vendor: string; model: string }
  | { type: 'start'; vendor: string; model: string; transport: 'http' | 'cli' }
  | { type: 'text_delta'; text: string }
  | { type: 'thinking_delta'; text: string }
  | { type: 'tool_call'; id: string; name: string; input: Record<string, unknown> }
  | { type: 'tool_result'; id: string; name: string; ok: boolean; preview: string }
  | {
      type: 'action_proposed';
      action_id: string;
      kind: string;
      summary: string;
      params: Record<string, unknown>;
      risk: 'low' | 'medium' | 'high';
    }
  | { type: 'action_output'; text: string }
  | { type: 'notice'; text: string }
  | { type: 'usage'; input_tokens: number; output_tokens: number }
  | { type: 'error'; message: string; retryable: boolean }
  | {
      type: 'done';
      stop_reason: string;
      text: string;
      session_id: string | null;
      awaiting: string | null;
    };

export interface PageContext {
  path: string;
  route: string;
  title?: string;
  entity?: {
    kind: 'task' | 'repo' | 'agent' | string;
    id?: string | number;
    key?: string;
    status?: string;
    name?: string;
  };
}

export interface VendorModel {
  id: string;
  label: string;
}

export interface AssistantCapabilities {
  edition: 'pro' | 'free';
  vendor: string | null;
  /** The Settings → System LLM model: the picker's default. */
  model: string | null;
  /** Models the picker may offer — the configured vendor's only. */
  models?: VendorModel[];
  tools: boolean;
  actions: boolean;
  workerUnreachable?: boolean;
}

export interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

/**
 * Consume an SSE response body, invoking `onEvent` per frame.
 *
 * Frames are separated by a blank line; only `data:` lines carry payload, and
 * `: ping` keep-alive comments are skipped. A partial frame at the end of a
 * chunk is held until the rest arrives.
 */
async function readEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: AssistantEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split = buffer.indexOf('\n\n');
    while (split !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        try {
          onEvent(JSON.parse(line.slice(6)) as AssistantEvent);
        } catch {
          /* a malformed frame must not kill the conversation */
        }
      }
      split = buffer.indexOf('\n\n');
    }
  }
}

async function postStream(
  url: string,
  payload: unknown,
  onEvent: (event: AssistantEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (err) {
    if (signal.aborted) return;
    onEvent({
      type: 'error',
      message: err instanceof Error ? err.message : 'network error',
      retryable: true,
    });
    return;
  }

  if (!res.ok || !res.body) {
    let message = `request failed (${res.status})`;
    try {
      const data = await res.json();
      if (data?.error) message = String(data.error);
    } catch {
      /* keep the status-based message */
    }
    onEvent({ type: 'error', message, retryable: res.status >= 500 });
    return;
  }

  try {
    await readEventStream(res.body, onEvent);
  } catch (err) {
    if (!signal.aborted) {
      onEvent({
        type: 'error',
        message: err instanceof Error ? err.message : 'stream interrupted',
        retryable: true,
      });
    }
  }
}

/** Send one turn and stream the answer back. */
export function askAgent(
  payload: {
    message: string;
    session_id?: string | null;
    repo_id?: number | null;
    mode?: 'auto' | 'chat' | 'agent';
    history: ChatTurn[];
    page?: PageContext | null;
    /** Model override for this turn; the worker falls back to the
     *  Settings value when absent or not valid for the vendor. */
    model?: string | null;
  },
  onEvent: (event: AssistantEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  return postStream('/api/assistant/chat', payload, onEvent, signal);
}

/** Approve or reject a proposed action; the outcome streams back (Pro). */
export function decideAction(
  actionId: string,
  decision: 'approve' | 'reject',
  onEvent: (event: AssistantEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  return postStream(
    `/api/assistant/actions/${encodeURIComponent(actionId)}/decide`,
    { decision, follow_up: true },
    onEvent,
    signal,
  );
}

export async function fetchCapabilities(): Promise<AssistantCapabilities> {
  try {
    const res = await fetch('/api/assistant/capabilities', { cache: 'no-store' });
    if (res.ok) return (await res.json()) as AssistantCapabilities;
  } catch {
    /* fall through to the safe default */
  }
  return {
    edition: 'free',
    vendor: null,
    model: null,
    tools: false,
    actions: false,
    workerUnreachable: true,
  };
}
