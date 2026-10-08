'use client';

import React from 'react';
import { CFormInput } from '@coreui/react-pro';

// Anthropic-only suggestion list for the per-repo default model (RepoForm).
// Mirrors the `anthropic` entry of AGENT_VENDORS in `@/lib/agent-vendors`;
// retired IDs (Claude 3.x — all 404 since Feb 2026) have been removed.
export const CLAUDE_MODELS = [
  { id: 'claude-opus-5-5',              label: 'Claude Opus 5.5 (flagship — agentic coding)' },
  { id: 'claude-sonnet-5-5',            label: 'Claude Sonnet 5.5 (fast, near-Opus quality)' },
  { id: 'claude-fable-5-1',             label: 'Claude Fable 5.1 (most capable, premium)' },
  { id: 'claude-opus-5',                label: 'Claude Opus 5' },
  { id: 'claude-sonnet-5',              label: 'Claude Sonnet 5' },
  { id: 'claude-fable-5',               label: 'Claude Fable 5' },
  { id: 'claude-opus-4-8',              label: 'Claude Opus 4.8' },
  { id: 'claude-opus-4-7',              label: 'Claude Opus 4.7' },
  { id: 'claude-opus-4-6',              label: 'Claude Opus 4.6' },
  { id: 'claude-sonnet-4-6',            label: 'Claude Sonnet 4.6' },
  { id: 'claude-haiku-4-5-20251001',    label: 'Claude Haiku 4.5' },
  { id: 'claude-opus-4-5',              label: 'Claude Opus 4.5' },
  { id: 'claude-sonnet-4-5',            label: 'Claude Sonnet 4.5' },
];

interface ModelComboboxProps {
  name: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}

export function ModelCombobox({ name, value, onChange, placeholder }: ModelComboboxProps) {
  const listId = `${name}-models`;
  return (
    <>
      <CFormInput
        name={name}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        list={listId}
        placeholder={placeholder ?? 'Type or select a model…'}
        autoComplete="off"
      />
      <datalist id={listId}>
        {CLAUDE_MODELS.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label}
          </option>
        ))}
      </datalist>
    </>
  );
}
