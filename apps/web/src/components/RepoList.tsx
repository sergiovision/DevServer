'use client';

import React from 'react';
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

interface RepoListProps {
  repos: Repo[];
}

export function RepoList({ repos }: RepoListProps) {
  const router = useRouter();

  return (
    <CTable hover responsive striped>
      <CTableHead>
        <CTableRow>
          <CTableHeaderCell>Name</CTableHeaderCell>
          <CTableHeaderCell>Owner/Repo</CTableHeaderCell>
          <CTableHeaderCell>Branch</CTableHeaderCell>
          <CTableHeaderCell>Model</CTableHeaderCell>
          <CTableHeaderCell>Status</CTableHeaderCell>
          <CTableHeaderCell>Actions</CTableHeaderCell>
        </CTableRow>
      </CTableHead>
      <CTableBody>
        {repos.length === 0 ? (
          <CTableRow>
            <CTableDataCell colSpan={6} className="text-center text-body-secondary">
              No repositories configured.
            </CTableDataCell>
          </CTableRow>
        ) : (
          repos.map((repo) => (
            <CTableRow key={repo.id}>
              <CTableDataCell><strong>{repo.name}</strong></CTableDataCell>
              <CTableDataCell>{repo.gitea_owner}/{repo.gitea_repo}</CTableDataCell>
              <CTableDataCell>{repo.default_branch}</CTableDataCell>
              <CTableDataCell>{repo.claude_model || '-'}</CTableDataCell>
              <CTableDataCell>
                <CBadge color={repo.active ? 'success' : 'secondary'}>
                  {repo.active ? 'Active' : 'Inactive'}
                </CBadge>
              </CTableDataCell>
              <CTableDataCell>
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
}
