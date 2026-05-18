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
    const result = await query<Repo>('SELECT * FROM repos WHERE id = $1', [repoId]);
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
