/** Internal worker base URL. Override with WORKER_URL env var for Docker deployments. */
export const WORKER_URL =
  process.env.WORKER_URL ||
  `http://localhost:${process.env.WORKER_PORT || 8000}`;
