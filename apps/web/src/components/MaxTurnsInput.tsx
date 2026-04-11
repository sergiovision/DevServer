'use client';

import React from 'react';
import { CFormInput } from '@coreui/react-pro';

export const MAX_TURNS_PRESETS = [
  { name: 'healthcheck',   value: 3 },
  { name: 'data_sync',     value: 10 },
  { name: 'code_review',   value: 15 },
  { name: 'report',        value: 20 },
  { name: 'research',      value: 30 },
  { name: 'default',       value: 50 },
  { name: 'big_task',      value: 100 },
  { name: 'long_running',  value: null },
] as const;

/** Parse the raw input string to number | null. Returns undefined if invalid. */
export function parseMaxTurns(raw: string): number | null | undefined {
  const trimmed = raw.trim();
  if (trimmed === '' || trimmed === 'Unlimited') return null;
  const n = parseInt(trimmed, 10);
  if (isNaN(n) || n <= 0) return undefined;
  return n;
}

interface MaxTurnsInputProps {
  name?: string;
  value: number | null;
  onChange: (value: number | null) => void;
  /** Show an inline error message */
  error?: string;
}

export function MaxTurnsInput({ name = 'max_turns', value, onChange, error }: MaxTurnsInputProps) {
  const listId = `${name}-presets`;
  const displayValue = value === null ? '' : String(value);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const parsed = parseMaxTurns(e.target.value);
    if (parsed !== undefined) onChange(parsed);
  };

  return (
    <>
      <CFormInput
        name={name}
        value={displayValue}
        onChange={handleChange}
        list={listId}
        placeholder="50 (default)"
        autoComplete="off"
        invalid={!!error}
        feedbackInvalid={error}
      />
      <datalist id={listId}>
        {MAX_TURNS_PRESETS.map((p) => (
          <option key={p.name} value={p.value === null ? '' : String(p.value)}>
            {p.name} — {p.value === null ? 'Unlimited' : `${p.value} turns`}
          </option>
        ))}
      </datalist>
    </>
  );
}
