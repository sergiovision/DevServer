/**
 * POST /api/version/update/dismiss — hide the "new version" notice for one release.
 *
 * Body `{build: "1.0.21"}` (the release's full `major.minor.build`). Stored in
 * the `update_dismissed_build` setting with the same JSONB upsert as
 * `PUT /api/settings`. Only that release is hidden: a later one compares
 * unequal and shows again.
 */

import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { jsonError } from '@/lib/route-utils';
import { DISMISSED_SETTING_KEY, isReleaseVersion } from '@/lib/update-notice';

export async function POST(request: NextRequest) {
  const body: unknown = await request.json().catch(() => null);
  const build = body && typeof body === 'object' ? (body as { build?: unknown }).build : undefined;
  if (!isReleaseVersion(build)) {
    return jsonError(400, 'build must be a release version like "1.0.21"');
  }
  try {
    await query(
      `INSERT INTO settings (key, value) VALUES ($1, $2)
       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
      [DISMISSED_SETTING_KEY, JSON.stringify(build)],
    );
    return NextResponse.json({ success: true, dismissed_build: build });
  } catch (err) {
    return apiErrorResponse(err, 'POST /api/version/update/dismiss');
  }
}
