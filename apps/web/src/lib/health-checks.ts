/**
 * Health check utilities for DevServer web application
 * Provides comprehensive health monitoring for all critical dependencies
 */

import { query } from '@/lib/db';
import { WORKER_URL } from '@/lib/worker-url';

export interface HealthCheck {
  name: string;
  status: 'healthy' | 'unhealthy' | 'degraded';
  responseTime?: number;
  error?: string;
  details?: Record<string, any>;
}

export interface HealthResponse {
  status: 'healthy' | 'unhealthy' | 'degraded';
  timestamp: string;
  checks: HealthCheck[];
  summary: {
    total: number;
    healthy: number;
    unhealthy: number;
    degraded: number;
  };
}

/**
 * Check PostgreSQL database connectivity
 */
export async function checkDatabase(): Promise<HealthCheck> {
  const start = Date.now();
  try {
    await query('SELECT 1 as health_check');
    const responseTime = Date.now() - start;

    return {
      name: 'database',
      status: responseTime > 5000 ? 'degraded' : 'healthy',
      responseTime,
      details: {
        host: process.env.PGHOST || '127.0.0.1',
        port: process.env.PGPORT || '5432',
        database: process.env.PGDATABASE || 'devserver'
      }
    };
  } catch (error) {
    return {
      name: 'database',
      status: 'unhealthy',
      responseTime: Date.now() - start,
      error: error instanceof Error ? error.message : String(error)
    };
  }
}

/**
 * Check DevServer worker service availability
 */
export async function checkWorkerService(): Promise<HealthCheck> {
  const start = Date.now();
  try {
    const response = await fetch(`${WORKER_URL}/health`, {
      signal: AbortSignal.timeout(5000)
    });

    const responseTime = Date.now() - start;
    const data = await response.json();

    if (response.ok) {
      return {
        name: 'worker_service',
        status: responseTime > 3000 ? 'degraded' : 'healthy',
        responseTime,
        details: {
          url: WORKER_URL,
          service: data.service || 'devserver-worker'
        }
      };
    } else {
      return {
        name: 'worker_service',
        status: 'unhealthy',
        responseTime,
        error: `HTTP ${response.status}: ${response.statusText}`
      };
    }
  } catch (error) {
    return {
      name: 'worker_service',
      status: 'unhealthy',
      responseTime: Date.now() - start,
      error: error instanceof Error ? error.message : String(error),
      details: { url: WORKER_URL }
    };
  }
}

/**
 * Check task queue system status
 */
export async function checkTaskQueue(): Promise<HealthCheck> {
  const start = Date.now();
  try {
    // Check queue table exists and get basic stats
    const queueStats = await query(`
      SELECT
        COUNT(*) as total_jobs,
        COUNT(*) FILTER (WHERE status = 'queued') as queued_jobs,
        COUNT(*) FILTER (WHERE status = 'picked') as running_jobs,
        COUNT(*) FILTER (WHERE status = 'exception') as failed_jobs
      FROM pgqueuer
      WHERE entrypoint = 'devserver-tasks'
    `);

    const responseTime = Date.now() - start;
    const stats = queueStats.rows[0];

    // Consider degraded if too many failed jobs
    const failureRate = parseInt(stats.failed_jobs) / Math.max(parseInt(stats.total_jobs), 1);
    const status = failureRate > 0.5 ? 'degraded' : 'healthy';

    return {
      name: 'task_queue',
      status,
      responseTime,
      details: {
        total_jobs: parseInt(stats.total_jobs),
        queued_jobs: parseInt(stats.queued_jobs),
        running_jobs: parseInt(stats.running_jobs),
        failed_jobs: parseInt(stats.failed_jobs),
        failure_rate: Math.round(failureRate * 100) / 100
      }
    };
  } catch (error) {
    return {
      name: 'task_queue',
      status: 'unhealthy',
      responseTime: Date.now() - start,
      error: error instanceof Error ? error.message : String(error)
    };
  }
}

/**
 * Check file system access for critical directories
 */
export async function checkFileSystem(): Promise<HealthCheck> {
  const start = Date.now();
  try {
    const fs = await import('fs/promises');
    const path = await import('path');

    // Test write access to temp directory
    const tempDir = process.env.WORKER_LOG || '/tmp';
    const testFile = path.join(tempDir, `health-check-${Date.now()}.tmp`);

    await fs.writeFile(testFile, 'health check', 'utf8');
    await fs.readFile(testFile, 'utf8');
    await fs.unlink(testFile);

    const responseTime = Date.now() - start;

    return {
      name: 'file_system',
      status: responseTime > 1000 ? 'degraded' : 'healthy',
      responseTime,
      details: {
        test_directory: tempDir,
        writable: true
      }
    };
  } catch (error) {
    return {
      name: 'file_system',
      status: 'unhealthy',
      responseTime: Date.now() - start,
      error: error instanceof Error ? error.message : String(error)
    };
  }
}

/**
 * Check external notification services (optional)
 */
export async function checkNotificationServices(): Promise<HealthCheck> {
  const start = Date.now();
  const services = [];

  // Check Telegram bot configuration
  if (process.env.TELEGRAM_BOT_TOKEN && process.env.TELEGRAM_CHAT_ID) {
    services.push('telegram');
  }

  // Check Discord webhook configuration
  if (process.env.DISCORD_WEBHOOK_URL) {
    services.push('discord');
  }

  if (services.length === 0) {
    return {
      name: 'notification_services',
      status: 'healthy',
      responseTime: Date.now() - start,
      details: {
        configured: false,
        message: 'No notification services configured (optional)'
      }
    };
  }

  // For configured services, we just verify configuration exists
  // Actual connectivity tests would require sending test messages
  return {
    name: 'notification_services',
    status: 'healthy',
    responseTime: Date.now() - start,
    details: {
      configured: true,
      services: services,
      message: 'Notification services configured (not tested)'
    }
  };
}

/**
 * Determine overall health status from individual checks
 */
export function calculateOverallStatus(checks: HealthCheck[]): 'healthy' | 'unhealthy' | 'degraded' {
  if (checks.some(check => check.status === 'unhealthy')) {
    return 'unhealthy';
  }
  if (checks.some(check => check.status === 'degraded')) {
    return 'degraded';
  }
  return 'healthy';
}

/**
 * Create health response summary
 */
export function createHealthSummary(checks: HealthCheck[]): HealthResponse['summary'] {
  return {
    total: checks.length,
    healthy: checks.filter(c => c.status === 'healthy').length,
    unhealthy: checks.filter(c => c.status === 'unhealthy').length,
    degraded: checks.filter(c => c.status === 'degraded').length
  };
}