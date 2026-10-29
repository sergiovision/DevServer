'use client';

import React from 'react';
import { CFormInput } from '@coreui/react-pro';
import { pickerModels } from '@/lib/agent-vendors';
import { useAgentRegistry } from '@/lib/use-agent-registry';

interface ModelComboboxProps {
  name: string;
  value: string;
  onChange: (value: string) => void;
  /** Vendor whose models are suggested. Defaults to Anthropic (repo default model). */
  vendor?: string;
  placeholder?: string;
}

/**
 * Free-text model input with suggestions from the worker's agent registry.
 * Any id may be typed; the suggestions only list the vendor's current models.
 */
export function ModelCombobox({
  name,
  value,
  onChange,
  vendor = 'anthropic',
  placeholder,
}: ModelComboboxProps) {
  const registry = useAgentRegistry();
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
        {pickerModels(registry, vendor).map((m) => (
          <option key={m.id} value={m.id}>
            {m.label}
          </option>
        ))}
      </datalist>
    </>
  );
}
