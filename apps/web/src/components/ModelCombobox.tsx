'use client';

import React from 'react';
import { CFormInput } from '@coreui/react-pro';

export const CLAUDE_MODELS = [
  { id: 'claude-opus-4-6',              label: 'Claude Opus 4.6 (most capable)' },
  { id: 'claude-sonnet-4-6',            label: 'Claude Sonnet 4.6' },
  { id: 'claude-haiku-4-5-20251001',    label: 'Claude Haiku 4.5' },
  { id: 'claude-opus-4-5',              label: 'Claude Opus 4.5' },
  { id: 'claude-sonnet-4-5',            label: 'Claude Sonnet 4.5' },
  { id: 'claude-3-7-sonnet-20250219',   label: 'Claude 3.7 Sonnet' },
  { id: 'claude-3-5-sonnet-20241022',   label: 'Claude 3.5 Sonnet' },
  { id: 'claude-3-5-haiku-20241022',    label: 'Claude 3.5 Haiku' },
  { id: 'claude-3-opus-20240229',       label: 'Claude 3 Opus' },
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
