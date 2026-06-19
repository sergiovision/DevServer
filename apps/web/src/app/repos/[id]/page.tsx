import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import { notFound } from 'next/navigation';
import type { Repo } from '@/lib/types';
import { RepoForm } from '@/components/RepoForm';

export const dynamic = 'force-dynamic';

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function RepoDetailPage({ params }: PageProps) {
  const { id } = await params;

  if (id === 'new') {
    return (
      <>
        <h2 className="mb-4">Add Repository</h2>
        <RepoForm />
      </>
    );
  }

  const repoId = parseInt(id);
  if (isNaN(repoId)) notFound();

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
            WHERE repo_id = $1
            GROUP BY repo_id
         ) cc ON cc.repo_id = r.id
        WHERE r.id = $1`,
      [repoId],
    );
    return result.rows[0] ?? null;
  });

  if (!r.ok) return r.panel;
  if (r.data === null) notFound();
  const repo = r.data;

  return (
    <>
      <h2 className="mb-4">Edit Repository: {repo.name}</h2>
      <RepoForm repo={repo} />
    </>
  );
}
