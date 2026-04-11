import { createServer } from 'http';
import { parse } from 'url';
import next from 'next';
import { WebSocketServer, WebSocket } from 'ws';
import { Client } from 'pg';

const dev = process.env.NODE_ENV !== 'production';
const hostname = '0.0.0.0';
const port = parseInt(process.env.PORT || '3000', 10);

const app = next({ dev, hostname, port });
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

  // PostgreSQL LISTEN/NOTIFY
  const pgClient = new Client({ connectionString: process.env.DATABASE_URL });
  pgClient.connect().then(() => {
    pgClient.query('LISTEN task_events');
    console.log('[ws] Listening on PG channel: task_events');
  }).catch((err) => {
    console.error('[ws] Failed to connect to PostgreSQL for LISTEN:', err.message);
  });

  pgClient.on('notification', (msg) => {
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
  });

  // Broadcast queue updates periodically
  let queueBroadcastInterval: NodeJS.Timeout | null = null;

  async function broadcastQueueStats() {
    try {
      const pgQuery = new Client({ connectionString: process.env.DATABASE_URL });
      await pgQuery.connect();
      const result = await pgQuery.query(`
        SELECT
          COUNT(*) FILTER (WHERE status = 'running') AS running,
          COUNT(*) FILTER (WHERE status = 'queued') AS queued,
          COUNT(*) FILTER (WHERE status = 'done' AND updated_at::date = CURRENT_DATE) AS completed
        FROM tasks
      `);
      await pgQuery.end();
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
    } catch {
      // ignore
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

  server.on('upgrade', (req, socket, head) => {
    const { pathname } = parse(req.url!, true);
    if (pathname === '/api/ws') {
      wss.handleUpgrade(req, socket, head, (ws) => {
        wss.emit('connection', ws, req);
      });
    } else {
      socket.destroy();
    }
  });

  // Start queue stats broadcast every 5 seconds
  queueBroadcastInterval = setInterval(broadcastQueueStats, 5000);

  server.listen(port, hostname, () => {
    console.log(`> Ready on http://${hostname}:${port}`);
  });

  // Cleanup
  process.on('SIGTERM', () => {
    if (queueBroadcastInterval) clearInterval(queueBroadcastInterval);
    pgClient.end();
    server.close();
  });
});
