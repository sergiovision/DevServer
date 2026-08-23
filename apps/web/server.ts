import { createServer, type IncomingMessage } from 'http';
import type { Duplex } from 'stream';
import { parse } from 'url';
import { readFileSync } from 'fs';
import { join } from 'path';
import next from 'next';
import { WebSocketServer, WebSocket } from 'ws';
import { Client, type Notification } from 'pg';

const dev = process.env.NODE_ENV !== 'production';
const hostname = '0.0.0.0';
const port = parseInt(process.env.WEB_PORT || process.env.PORT || '3200', 10);

// `output: 'standalone'` does NOT copy next.config.ts into the bundle — Next's
// own generated entrypoint inlines the resolved config instead. We replace that
// entrypoint (we need the /api/ws upgrade handler), so we have to reproduce the
// same bootstrap or `next()` silently falls back to framework defaults and
// drops serverActions.bodySizeLimit, the A2A agent-card rewrite, etc.
//
// `.next/required-server-files.json` is the file Next itself builds that inline
// config from, and it ships inside the standalone output. Read it, hand it to
// `next({ conf })`, and mirror it into the env var some internals read directly.
function loadStandaloneConfig(): Record<string, unknown> | undefined {
  if (dev) return undefined;
  try {
    const p = join(__dirname, '.next', 'required-server-files.json');
    const { config } = JSON.parse(readFileSync(p, 'utf-8'));
    if (!config) return undefined;
    process.env.__NEXT_PRIVATE_STANDALONE_CONFIG = JSON.stringify(config);
    return config;
  } catch {
    // Not a standalone build (e.g. `next build` + next.config.ts on disk).
    // Next will load the config file itself.
    return undefined;
  }
}

const conf = loadStandaloneConfig();

const app = next({ dev, hostname, port, dir: __dirname, webpack: true, ...(conf ? { conf } : {}) });
const handle = app.getRequestHandler();

interface WsClient {
  ws: WebSocket;
  subscribedTaskIds: Set<number>;
}

app.prepare().then(() => {
  const server = createServer((req, res) => {
    const parsedUrl = parse(req.url!, true);
    handle(req, res, parsedUrl);
  });

  const wss = new WebSocketServer({ noServer: true });
  const clients: Set<WsClient> = new Set();

  // PostgreSQL LISTEN/NOTIFY.
  //
  // The client MUST carry an 'error' listener. pg emits 'error' on the client
  // when the connection drops (ECONNRESET, server restart), and an
  // EventEmitter 'error' with no listener is an uncaught exception — it takes
  // the whole server down and dumps the entire Client object to the log.
  // That is exactly how this process died before the handler existed.
  //
  // A dropped connection also has to be re-established: `LISTEN` lives on the
  // connection, so without reconnecting, every live task update stops silently
  // even if the process survives.
  let pgClient: Client;
  let listenRetry: NodeJS.Timeout | null = null;

  function scheduleListenReconnect() {
    if (listenRetry) return; // collapse the connect-failure + 'error' pair
    listenRetry = setTimeout(() => {
      listenRetry = null;
      connectListener();
    }, 5000);
  }

  function connectListener() {
    pgClient = new Client({ connectionString: process.env.DATABASE_URL });
    pgClient.on('notification', handleNotification);
    pgClient.on('error', (err) => {
      console.error('[ws] PG LISTEN connection error:', err.message);
      scheduleListenReconnect();
    });
    pgClient.connect().then(() => {
      pgClient.query('LISTEN task_events');
      console.log('[ws] Listening on PG channel: task_events');
    }).catch((err) => {
      console.error('[ws] Failed to connect to PostgreSQL for LISTEN:', err.message);
      scheduleListenReconnect();
    });
  }

  function handleNotification(msg: Notification) {
    if (msg.channel !== 'task_events' || !msg.payload) return;
    try {
      const event = JSON.parse(msg.payload);
      const taskId = event.task_id;

      for (const client of clients) {
        if (client.ws.readyState !== WebSocket.OPEN) continue;
        if (client.subscribedTaskIds.has(taskId) || client.subscribedTaskIds.has(0)) {
          client.ws.send(JSON.stringify({
            type: 'task_event',
            taskId,
            eventType: event.event_type,
            payload: event.payload,
          }));
        }
      }
    } catch {
      // ignore malformed payloads
    }
  }

  connectListener();

  // Broadcast queue updates periodically
  let queueBroadcastInterval: NodeJS.Timeout | null = null;

  async function broadcastQueueStats() {
    // A fresh short-lived client every 5s. `end()` has to run in a `finally`:
    // when the query threw, the old code skipped `end()` and swallowed the
    // error, leaking a socket and its buffers on every tick until V8 died with
    // "Fatal process out of memory". The 'error' listener is required for the
    // same reason as on the LISTEN client — without it an async socket error
    // is an uncaught exception.
    const pgQuery = new Client({ connectionString: process.env.DATABASE_URL });
    pgQuery.on('error', (err) => {
      console.error('[ws] queue-stats client error:', err.message);
    });
    try {
      await pgQuery.connect();
      const result = await pgQuery.query(`
        SELECT
          COUNT(*) FILTER (WHERE status = 'running') AS running,
          COUNT(*) FILTER (WHERE status = 'queued') AS queued,
          COUNT(*) FILTER (WHERE status = 'done' AND updated_at::date = CURRENT_DATE) AS completed
        FROM tasks
      `);
      const stats = result.rows[0];
      const msg = JSON.stringify({
        type: 'queue_update',
        stats: {
          running: parseInt(stats.running),
          queued: parseInt(stats.queued),
          completed: parseInt(stats.completed),
        },
      });
      for (const client of clients) {
        if (client.ws.readyState === WebSocket.OPEN) {
          client.ws.send(msg);
        }
      }
    } catch (err) {
      console.error('[ws] queue-stats broadcast failed:', (err as Error).message);
    } finally {
      await pgQuery.end().catch(() => { /* already closed / never connected */ });
    }
  }

  wss.on('connection', (ws) => {
    const client: WsClient = { ws, subscribedTaskIds: new Set([0]) };
    clients.add(client);

    ws.on('message', (data) => {
      try {
        const msg = JSON.parse(data.toString());
        if (msg.type === 'subscribe' && Array.isArray(msg.taskIds)) {
          for (const id of msg.taskIds) {
            client.subscribedTaskIds.add(Number(id));
          }
        } else if (msg.type === 'unsubscribe' && Array.isArray(msg.taskIds)) {
          for (const id of msg.taskIds) {
            client.subscribedTaskIds.delete(Number(id));
          }
        }
      } catch {
        // ignore
      }
    });

    ws.on('close', () => {
      clients.delete(client);
    });
  });

  // On the first HTTP request Next lazily attaches its OWN 'upgrade' listener
  // to this server (server/next.js → setupWebSocketHandler). EventEmitter runs
  // every listener, so from that point Next's handler also sees `/api/ws`
  // upgrades — it does not recognise the path and destroys the socket. The
  // browser connects, gets a 1006 close a moment later, and every live task
  // update stops. It is ordering-dependent, which is how it survived this long:
  // connect before the first page load and it works, connect after and it never
  // does. Under Docker the HEALTHCHECK guarantees the bad ordering, so live
  // updates were dead on arrival there.
  //
  // Rather than fight two listeners, intercept the registration: keep exactly
  // one 'upgrade' listener on the server (ours) and hold Next's to one side, so
  // we decide what reaches it. Delegating to the captured listener — instead of
  // to the public getUpgradeHandler(), which resolves to a *different* handler
  // that never completes the dev HMR handshake — means Next's own paths behave
  // exactly as they would have.
  let nextUpgrade: ((req: IncomingMessage, socket: Duplex, head: Buffer) => void) | null = null;
  const serverOn = server.on.bind(server);
  server.on = ((event: string, listener: (...args: never[]) => void) => {
    if (event === 'upgrade') {
      nextUpgrade = listener as unknown as typeof nextUpgrade;
      return server;
    }
    return serverOn(event as 'request', listener as never);
  }) as typeof server.on;

  serverOn('upgrade', (req, socket, head) => {
    const { pathname } = parse(req.url!, true);
    if (pathname === '/api/ws') {
      wss.handleUpgrade(req, socket, head, (ws) => {
        wss.emit('connection', ws, req);
      });
      return;
    }
    // Everything else is Next's — in practice /_next/webpack-hmr in dev.
    nextUpgrade?.(req, socket, head);
  });

  // Start queue stats broadcast every 5 seconds
  queueBroadcastInterval = setInterval(broadcastQueueStats, 5000);

  server.listen(port, hostname, () => {
    console.log(`> Ready on http://${hostname}:${port}`);
  });

  // Cleanup
  process.on('SIGTERM', () => {
    if (queueBroadcastInterval) clearInterval(queueBroadcastInterval);
    if (listenRetry) clearTimeout(listenRetry);
    pgClient?.end().catch(() => { /* already down */ });
    server.close();
  });
});
