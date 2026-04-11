import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';

export async function GET() {
  try {
    const result = await query('SELECT * FROM repos ORDER BY name');
    return NextResponse.json(result.rows);
  } catch (err) {
    console.error('GET /api/repos error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
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

    if (!name || !clone_url) {
      return NextResponse.json(
        { error: 'name and clone_url are required' },
        { status: 400 },
      );
    }

    const result = await query(
      `INSERT INTO repos (
        name, gitea_url, gitea_owner, gitea_repo, clone_url, default_branch,
        build_cmd, test_cmd, lint_cmd, pre_cmd, claude_model, claude_allowed_tools,
        gitea_token, max_retries, timeout_minutes, active
      ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
      RETURNING *`,
      [
        name, gitea_url || null, gitea_owner || null, gitea_repo || null, clone_url,
        default_branch, build_cmd || null, test_cmd || null, lint_cmd || null, pre_cmd || null,
        claude_model || null, claude_allowed_tools || null, gitea_token || '',
        max_retries, timeout_minutes, active,
      ],
    );

    return NextResponse.json(result.rows[0], { status: 201 });
  } catch (err) {
    console.error('POST /api/repos error:', err);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
