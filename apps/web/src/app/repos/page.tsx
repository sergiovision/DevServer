import { query } from '@/lib/db';
import type { Repo } from '@/lib/types';
import { RepoList } from '@/components/RepoList';
import Link from 'next/link';

export const dynamic = 'force-dynamic';

export default async function ReposPage() {
  let repos: Repo[] = [];

  try {
    const result = await query<Repo>('SELECT * FROM repos ORDER BY name');
    repos = result.rows;
  } catch (err) {
    console.error('Failed to fetch repos:', err);
  }

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <Link href="/repos/new" className="btn btn-primary">
          + Add Repository
        </Link>
        <h2 className="mb-0">Repositories</h2>
      </div>
      <RepoList repos={repos} />
    </>
  );
}
