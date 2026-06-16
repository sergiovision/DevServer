/**
 * Readiness endpoint for DevServer web application
 * Checks if the application is ready to serve traffic by verifying all critical dependencies
 *
 * GET /api/ready
 * - 200: Application ready to serve requests
 * - 503: Application not ready (dependencies unavailable)
 *
 * This endpoint is more strict than /api/health and focuses on service availability
 * rather than degraded performance.
 */

import { NextResponse } from 'next/server';
import {
  checkDatabase,
  checkWorkerService,
  checkTaskQueue,
  type HealthResponse
} from '@/lib/health-checks';

export const dynamic = 'force-dynamic';

export async function GET() {
  const timestamp = new Date().toISOString();

  try {
    // Only check critical dependencies for readiness
    // File system and notifications are not blocking for basic functionality
    const [
      databaseCheck,
      workerCheck,
      queueCheck
    ] = await Promise.all([
      checkDatabase(),
      checkWorkerService(),
      checkTaskQueue()
    ]);

    const checks = [databaseCheck, workerCheck, queueCheck];

    // For readiness, we're more strict - any unhealthy critical service = not ready
    const criticalIssues = checks.filter(check => check.status === 'unhealthy');
    const isReady = criticalIssues.length === 0;

    const summary = {
      total: checks.length,
      healthy: checks.filter(c => c.status === 'healthy').length,
      unhealthy: checks.filter(c => c.status === 'unhealthy').length,
      degraded: checks.filter(c => c.status === 'degraded').length
    };

    const response: HealthResponse = {
      status: isReady ? 'healthy' : 'unhealthy',
      timestamp,
      checks,
      summary
    };

    // Add readiness-specific metadata
    const responseWithReadiness = {
      ...response,
      ready: isReady,
      critical_issues: criticalIssues.map(check => ({
        service: check.name,
        error: check.error
      }))
    };

    return NextResponse.json(responseWithReadiness, {
      status: isReady ? 200 : 503,
      headers: {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'X-Ready-Status': isReady ? 'ready' : 'not-ready'
      }
    });

  } catch (error) {
    // If readiness check fails, definitely not ready
    const fallbackResponse = {
      status: 'unhealthy',
      ready: false,
      timestamp,
      checks: [{
        name: 'readiness_system',
        status: 'unhealthy' as const,
        error: error instanceof Error ? error.message : String(error)
      }],
      summary: {
        total: 1,
        healthy: 0,
        unhealthy: 1,
        degraded: 0
      },
      critical_issues: [{
        service: 'readiness_system',
        error: error instanceof Error ? error.message : String(error)
      }]
    };

    return NextResponse.json(fallbackResponse, {
      status: 503,
      headers: {
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'X-Ready-Status': 'not-ready'
      }
    });
  }
}