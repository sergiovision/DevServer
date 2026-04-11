import { Pool, QueryResult, QueryResultRow } from 'pg';

// PGPASSWORD is required — we deliberately do NOT ship a hardcoded fallback.
// Missing the env var at boot is a configuration error, not something we want
// to paper over with a default password that could end up in a container image.
if (!process.env.PGPASSWORD) {
  console.error(
    'PGPASSWORD is not set. Configure .env (copy from config/.env.example) before starting the web app.',
  );
}

const pool = new Pool({
  host: process.env.PGHOST || '127.0.0.1',
  port: parseInt(process.env.PGPORT || '5432'),
  user: process.env.PGUSER || 'devserver',
  password: process.env.PGPASSWORD || '',
  database: process.env.PGDATABASE || 'devserver',
  max: 20,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000,
});

pool.on('error', (err) => {
  console.error('Unexpected PostgreSQL pool error:', err);
});

export async function query<T extends QueryResultRow = QueryResultRow>(
  text: string,
  params?: unknown[],
): Promise<QueryResult<T>> {
  const start = Date.now();
  const result = await pool.query<T>(text, params);
  const duration = Date.now() - start;
  if (duration > 1000) {
    console.warn(`Slow query (${duration}ms):`, text.slice(0, 100));
  }
  return result;
}

export { pool };
export default pool;
