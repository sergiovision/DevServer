/**
 * Health endpoint for DevServer web application
 * Returns overall application health status based on critical dependencies
 *
 * GET /api/health
 * - 200: All systems healthy
 * - 206: Some systems degraded but functional
 * - 503: Critical systems unhealthy
 */

import { NextResponse } from 'next/server';
import {
  checkDatabase,
  checkWorkerService,
  checkTaskQueue,
  checkFileSystem,
  checkNotificationServices,
  calculateOverallStatus,
  createHealthSummary,
  type HealthResponse
} from '@/lib/health-checks';

export const dynamic = 'force-dynamic';

export async function GET() {
  const timestamp = new Date().toISOString();

  try {
    // Run all health checks in parallel for faster response
    const [
      databaseCheck,
      workerCheck,
      queueCheck,
      filesystemCheck,
      notificationCheck
    ] = await Promise.all([
      checkDatabase(),
      checkWorkerService(),
      checkTaskQueue(),
      checkFileSystem(),
      checkNotificationServices()
    ]);

    const checks = [
      databaseCheck,
      workerCheck,
      queueCheck,
      filesystemCheck,
      notificationCheck
    ];

    const overallStatus = calculateOverallStatus(checks);
    const summary = createHealthSummary(checks);

    const response: HealthResponse = {
      status: overallStatus,
      timestamp,
      checks,
      summary
    };

    // Return appropriate HTTP status code
    const statusCode = overallStatus === 'healthy' ? 200
                     : overallStatus === 'degraded' ? 206
                     : 503;

    return NextResponse.json(response, {
      status: statusCode,
      headers: {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'X-Health-Status': overallStatus
      }
    });

  } catch (error) {
    // Fallback response if health check system itself fails
    const fallbackResponse: HealthResponse = {
      status: 'unhealthy',
      timestamp,
      checks: [{
        name: 'health_system',
        status: 'unhealthy',
        error: error instanceof Error ? error.message : String(error)
      }],
      summary: {
        total: 1,
        healthy: 0,
        unhealthy: 1,
        degraded: 0
      }
    };

    return NextResponse.json(fallbackResponse, {
      status: 503,
      headers: {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'X-Health-Status': 'unhealthy'
      }
    });
  }
}