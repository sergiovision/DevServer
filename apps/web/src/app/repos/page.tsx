import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import type { Repo } from '@/lib/types';
import { RepoList } from '@/components/RepoList';
import Link from 'next/link';

export const dynamic = 'force-dynamic';

export default async function ReposPage() {
  const r = await tryDbPage(async () => {
    const result = await query<Repo>(
      `SELECT r.*,
              COALESCE(cc.code_chunks, 0)::int AS corpus_code_chunks,
              COALESCE(cc.code_files, 0)::int  AS corpus_code_files,
              COALESCE(cc.doc_chunks, 0)::int  AS corpus_doc_chunks,
              COALESCE(cc.doc_files, 0)::int   AS corpus_doc_files
         FROM repos r
         LEFT JOIN (
           SELECT repo_id,
                  count(*) FILTER (WHERE kind = 'code')             AS code_chunks,
                  count(DISTINCT path) FILTER (WHERE kind = 'code') AS code_files,
                  count(*) FILTER (WHERE kind = 'doc')              AS doc_chunks,
                  count(DISTINCT path) FILTER (WHERE kind = 'doc')  AS doc_files
             FROM corpus_chunks
            GROUP BY repo_id
         ) cc ON cc.repo_id = r.id
        ORDER BY r.name`
    );
    return result.rows;
  });

  if (!r.ok) return r.panel;

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <Link href="/repos/new" className="btn btn-primary">
          + Add Repository
        </Link>
        <h2 className="mb-0">Repositories</h2>
      </div>
      <RepoList repos={r.data} />
    </>
  );
}
