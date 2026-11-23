import { NextRequest, NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { apiErrorResponse } from '@/lib/api-errors';
import { invalidId, jsonError, parseRouteId, type RouteContext } from '@/lib/route-utils';

/**
 * PATCH  /api/ideas/<id>   body: any subset of the UPDATABLE columns
 * DELETE /api/ideas/<id>
 *
 * Edit or remove one Ideas / Goal Graph node. Only allow-listed columns can be
 * written, so the dynamic SET clause never takes a column name from the body.
 */

const UPDATABLE = new Set([
  'parent_id',
  'title',
  'content',
  'tasked',
  'task_id',
  'sort_order',
  'node_type',
  'node_status',
]);

export async function PATCH(request: NextRequest, { params }: RouteContext) {
  try {
    const { id } = await params;
    const ideaId = parseRouteId(id);
    if (ideaId === null) return invalidId();

    const body = await request.json();
    const fields: string[] = [];
    const values: unknown[] = [];
    let i = 1;
    for (const [key, value] of Object.entries(body)) {
      if (!UPDATABLE.has(key)) continue;
      fields.push(`${key} = $${i++}`);
      values.push(value);
    }

    if (fields.length === 0) {
      return jsonError(400, 'No updatable fields provided');
    }

    values.push(ideaId);
    const sql = `UPDATE ideas SET ${fields.join(', ')} WHERE id = $${i} RETURNING *`;
    const result = await query(sql, values);
    if (result.rows.length === 0) {
      return jsonError(404, 'Not found');
    }
    return NextResponse.json(result.rows[0]);
  } catch (err) {
    return apiErrorResponse(err, 'PATCH /api/ideas/[id]');
  }
}

export async function DELETE(_request: NextRequest, { params }: RouteContext) {
  try {
    const { id } = await params;
    const ideaId = parseRouteId(id);
    if (ideaId === null) return invalidId();
    const result = await query('DELETE FROM ideas WHERE id = $1 RETURNING id', [ideaId]);
    if (result.rows.length === 0) {
      return jsonError(404, 'Not found');
    }
    return NextResponse.json({ ok: true });
  } catch (err) {
    return apiErrorResponse(err, 'DELETE /api/ideas/[id]');
  }
}
