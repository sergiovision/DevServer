'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  CTable,
  CTableHead,
  CTableRow,
  CTableHeaderCell,
  CTableBody,
  CTableDataCell,
  CBadge,
  CButton,
} from '@coreui/react-pro';
import type { Repo } from '@/lib/types';
import { RepoDiagramModal } from './RepoDiagramModal';

interface RepoListProps {
  repos: Repo[];
}

// Render a "chunks / files" corpus stat, or a muted dash when nothing is indexed.
function corpusCell(chunks?: number, files?: number) {
  if (!chunks) {
    return <span className="text-body-secondary">—</span>;
  }
  return (
    <span className="font-monospace small" title={`${chunks} chunks across ${files} files`}>
      {chunks.toLocaleString()} / {(files ?? 0).toLocaleString()}
    </span>
  );
}

export function RepoList({ repos }: RepoListProps) {
  const router = useRouter();
  const [diagramRepo, setDiagramRepo] = useState<Repo | null>(null);

  const table = (
    <CTable hover responsive striped>
      <CTableHead>
        <CTableRow>
          <CTableHeaderCell>Name</CTableHeaderCell>
          <CTableHeaderCell>Owner/Repo</CTableHeaderCell>
          <CTableHeaderCell>Branch</CTableHeaderCell>
          <CTableHeaderCell>Model</CTableHeaderCell>
          <CTableHeaderCell title="Corpus code chunks / files indexed">Code</CTableHeaderCell>
          <CTableHeaderCell title="Corpus doc chunks / files indexed">Doc</CTableHeaderCell>
          <CTableHeaderCell>Status</CTableHeaderCell>
          <CTableHeaderCell>Actions</CTableHeaderCell>
        </CTableRow>
      </CTableHead>
      <CTableBody>
        {repos.length === 0 ? (
          <CTableRow>
            <CTableDataCell colSpan={8} className="text-center text-body-secondary">
              No repositories configured.
            </CTableDataCell>
          </CTableRow>
        ) : (
          repos.map((repo) => (
            <CTableRow key={repo.id}>
              <CTableDataCell><strong>{repo.name}</strong></CTableDataCell>
              <CTableDataCell>
                {repo.provider === 'local' ? (
                  <>
                    <CBadge
                      className="me-2"
                      style={{ backgroundColor: '#1abc9c', color: '#fff' }}
                    >
                      Local
                    </CBadge>
                    <code className="small" style={{ color: '#1abc9c' }}>{repo.gitea_url}</code>
                  </>
                ) : (
                  <>
                    <CBadge color={repo.provider === 'github' ? 'dark' : 'info'} className="me-2">
                      {repo.provider === 'github' ? 'GitHub' : 'Gitea'}
                    </CBadge>
                    {repo.gitea_owner}/{repo.gitea_repo}
                  </>
                )}
              </CTableDataCell>
              <CTableDataCell>{repo.default_branch}</CTableDataCell>
              <CTableDataCell>{repo.claude_model || '-'}</CTableDataCell>
              <CTableDataCell>{corpusCell(repo.corpus_code_chunks, repo.corpus_code_files)}</CTableDataCell>
              <CTableDataCell>{corpusCell(repo.corpus_doc_chunks, repo.corpus_doc_files)}</CTableDataCell>
              <CTableDataCell>
                <CBadge color={repo.active ? 'success' : 'secondary'}>
                  {repo.active ? 'Active' : 'Inactive'}
                </CBadge>
              </CTableDataCell>
              <CTableDataCell>
                <CButton
                  size="sm"
                  color="outline-secondary"
                  className="me-1"
                  onClick={() => setDiagramRepo(repo)}
                  title="Architecture diagram (module tree)"
                >
                  Diagram
                </CButton>
                <CButton
                  size="sm"
                  color="outline-primary"
                  onClick={() => router.push(`/repos/${repo.id}`)}
                >
                  Edit
                </CButton>
              </CTableDataCell>
            </CTableRow>
          ))
        )}
      </CTableBody>
    </CTable>
  );

  return (
    <>
      {table}
      {diagramRepo && (
        <RepoDiagramModal
          repoId={diagramRepo.id}
          repoName={diagramRepo.name}
          visible={!!diagramRepo}
          onClose={() => setDiagramRepo(null)}
        />
      )}
    </>
  );
}
