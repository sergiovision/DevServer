/**
 * A2A 1.0 protocol types + JSON-RPC 2.0 envelope helpers.
 *
 * Hand-rolled rather than pulled from an SDK: the server side is ~6 methods,
 * and the spec moved 0.1 → 0.3 → 1.0 quickly enough that a pinned SDK is a
 * liability. This file is the single place the wire format is defined.
 *
 * WIRE-FORMAT NOTE — this bit is easy to get wrong. The A2A 1.0 data model is
 * defined in Protocol Buffers, where the enums are SCREAMING_SNAKE
 * (``TASK_STATE_SUBMITTED``, ``ROLE_USER``). The JSON-RPC binding does NOT use
 * those names: every shipping implementation (AWS Bedrock AgentCore, LangChain
 * Agent Server, the a2a-sdk clients) puts lowercase kebab values on the wire —
 * ``"submitted"``, ``"input-required"``, ``"completed"``, and ``"user"`` /
 * ``"agent"`` for roles. Parts are discriminated by a ``kind`` field rather
 * than the proto oneof. We follow the wire format, because interop is the
 * whole point of implementing this.
 */

export const A2A_PROTOCOL_VERSION = '1.0';

// ─── Task state ──────────────────────────────────────────────────────────────

export type A2ATaskState =
  | 'submitted'
  | 'working'
  | 'input-required'
  | 'completed'
  | 'failed'
  | 'canceled'
  | 'rejected'
  | 'auth-required'
  | 'unknown';

/** States a task can never leave — used to reject cancel/steer attempts. */
export const A2A_TERMINAL_STATES: ReadonlySet<A2ATaskState> = new Set<A2ATaskState>([
  'completed',
  'failed',
  'canceled',
  'rejected',
]);

// ─── Parts / messages / artifacts ────────────────────────────────────────────

export type A2APart =
  | { kind: 'text'; text: string; metadata?: Record<string, unknown> }
  | { kind: 'data'; data: unknown; metadata?: Record<string, unknown> }
  | {
      kind: 'file';
      file: { uri?: string; bytes?: string; name?: string; mimeType?: string };
      metadata?: Record<string, unknown>;
    };

export interface A2AMessage {
  kind: 'message';
  messageId: string;
  role: 'user' | 'agent';
  parts: A2APart[];
  taskId?: string;
  contextId?: string;
  metadata?: Record<string, unknown>;
}

export interface A2AArtifact {
  artifactId: string;
  name?: string;
  description?: string;
  parts: A2APart[];
  metadata?: Record<string, unknown>;
}

export interface A2ATaskStatus {
  state: A2ATaskState;
  timestamp?: string;
  message?: A2AMessage;
}

export interface A2ATask {
  kind: 'task';
  id: string;
  contextId?: string;
  status: A2ATaskStatus;
  artifacts?: A2AArtifact[];
  history?: A2AMessage[];
  metadata?: Record<string, unknown>;
}

// ─── Agent Card ──────────────────────────────────────────────────────────────

export interface A2ASkill {
  id: string;
  name: string;
  description: string;
  tags: string[];
  examples?: string[];
  inputModes?: string[];
  outputModes?: string[];
}

export interface A2AAgentCard {
  name: string;
  description: string;
  version: string;
  protocolVersion: string;
  url: string;
  preferredTransport: string;
  provider?: { organization: string; url?: string };
  capabilities: {
    streaming: boolean;
    pushNotifications: boolean;
    stateTransitionHistory?: boolean;
  };
  defaultInputModes: string[];
  defaultOutputModes: string[];
  securitySchemes?: Record<string, unknown>;
  security?: Record<string, string[]>[];
  skills: A2ASkill[];
}

// ─── JSON-RPC 2.0 ────────────────────────────────────────────────────────────

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id?: string | number | null;
  method: string;
  params?: unknown;
}

/** Standard JSON-RPC codes plus the A2A-specific range (§9 of the spec). */
export const RPC = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
  // A2A-specific
  TASK_NOT_FOUND: -32001,
  TASK_NOT_CANCELABLE: -32002,
  PUSH_NOTIFICATION_NOT_SUPPORTED: -32003,
  UNSUPPORTED_OPERATION: -32004,
  CONTENT_TYPE_NOT_SUPPORTED: -32005,
} as const;

export function rpcResult(id: string | number | null | undefined, result: unknown) {
  return { jsonrpc: '2.0' as const, id: id ?? null, result };
}

export function rpcError(
  id: string | number | null | undefined,
  code: number,
  message: string,
  data?: unknown,
) {
  return {
    jsonrpc: '2.0' as const,
    id: id ?? null,
    error: data === undefined ? { code, message } : { code, message, data },
  };
}

/**
 * Thrown by handlers to produce a JSON-RPC error response. Using an exception
 * keeps the happy path in each handler linear instead of threading result
 * unions through every helper.
 */
export class RpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
    readonly data?: unknown,
  ) {
    super(message);
    this.name = 'RpcError';
  }
}

// ─── Part helpers ────────────────────────────────────────────────────────────

export function textPart(text: string): A2APart {
  return { kind: 'text', text };
}

export function dataPart(data: unknown): A2APart {
  return { kind: 'data', data };
}

export function filePart(uri: string, name?: string, mimeType?: string): A2APart {
  return { kind: 'file', file: { uri, name, mimeType } };
}

/**
 * Concatenate the text of every text Part in a message. Non-text parts are
 * ignored — DevServer tasks are described in prose, and silently dropping a
 * data blob is better than stringifying JSON into a task description.
 */
export function partsToText(parts: unknown): string {
  if (!Array.isArray(parts)) return '';
  return parts
    .filter(
      (p): p is { kind: 'text'; text: string } =>
        !!p &&
        typeof p === 'object' &&
        (p as { kind?: unknown }).kind === 'text' &&
        typeof (p as { text?: unknown }).text === 'string',
    )
    .map((p) => p.text)
    .join('\n\n')
    .trim();
}
