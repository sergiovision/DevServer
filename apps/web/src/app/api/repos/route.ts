import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';

export async function GET() {
  try {
    const result = await query('SELECT * FROM repos ORDER BY name');
    return NextResponse.json(result.rows);
  } catch (err) {
    return apiErrorResponse(err, 'GET /api/repos');
  }
}

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const {
      name, gitea_url, gitea_owner, gitea_repo, clone_url,
      default_branch = 'main', build_cmd, test_cmd, lint_cmd, pre_cmd,
      claude_model, claude_allowed_tools, gitea_token, max_retries = 3,
      timeout_minutes = 30, active = true,
    } = body;

    // Provider: honour an explicit value, else sniff the clone URL host so a
    // github.com repo is never silently treated as Gitea (which would auth
    // with the wrong scheme and fail to clone). 'local' is never sniffed —
    // it must be chosen explicitly.
    const rawProvider = (body.provider || '').toString().trim().toLowerCase();
    const provider =
      rawProvider === 'github' || rawProvider === 'gitea' || rawProvider === 'local'
        ? rawProvider
        : /^https?:\/\/([^/@]+@)?github\.com\//i.test(clone_url || '')
          ? 'github'
          : 'gitea';

    if (!name) {
      return NextResponse.json({ error: 'name is required' }, { status: 400 });
    }
    if (provider === 'local') {
      // Local repos are defined by their folder (stored in gitea_url, the
      // "Local Root Folder") — a clone URL is meaningless for them.
      if (!gitea_url || !gitea_url.toString().trim()) {
        return NextResponse.json(
          { error: 'gitea_url (Local Root Folder) is required for a Local Git repository' },
          { status: 400 },
        );
      }
    } else if (!clone_url) {
      return NextResponse.json(
        { error: 'name and clone_url are required' },
        { status: 400 },
      );
    }

    const result = await query(
      `INSERT INTO repos (
        name, gitea_url, gitea_owner, gitea_repo, clone_url, default_branch,
        build_cmd, test_cmd, lint_cmd, pre_cmd, claude_model, claude_allowed_tools,
        gitea_token, provider, max_retries, timeout_minutes, active
      ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
      RETURNING *`,
      [
        // gitea_url / gitea_owner / gitea_repo / clone_url are NOT NULL
        // varchar columns in deployed databases — always write '' for
        // blanks (a Local Git repo legitimately has no owner/repo/clone).
        name, gitea_url || '', gitea_owner || '', gitea_repo || '', clone_url || '',
        default_branch, build_cmd || null, test_cmd || null, lint_cmd || null, pre_cmd || null,
        claude_model || null, claude_allowed_tools || null, gitea_token || '', provider,
        max_retries, timeout_minutes, active,
      ],
    );

    return NextResponse.json(result.rows[0], { status: 201 });
  } catch (err) {
    return apiErrorResponse(err, 'POST /api/repos');
  }
}
