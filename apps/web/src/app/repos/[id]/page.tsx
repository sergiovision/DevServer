import { query } from '@/lib/db';
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

  let repo: Repo | null = null;

  try {
    const result = await query<Repo>('SELECT * FROM repos WHERE id = $1', [repoId]);
    if (result.rows.length === 0) notFound();
    repo = result.rows[0];
  } catch (err) {
    console.error('Failed to fetch repo:', err);
    notFound();
  }

  return (
    <>
      <h2 className="mb-4">Edit Repository: {repo!.name}</h2>
      <RepoForm repo={repo!} />
    </>
  );
}
