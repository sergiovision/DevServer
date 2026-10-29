/**
 * VendorModelPicker — two linked controls (vendor + model) for the
 * AgentBackend abstraction.
 *
 * Left: a select of the vendors the worker's registry knows. Right: a
 * free-text input with a datalist of that vendor's current models, so users
 * can pick a suggestion or type any id (a pinned/dated model, or one that has
 * since been deprecated and is therefore no longer suggested).
 *
 * Changing the vendor resets the model to the new vendor's default, unless
 * the current model id is also registered under the new vendor.
 *
 * While the registry is loading the controls still render the stored values;
 * the vendor list and suggestions fill in once it arrives.
 */
'use client';

import React, { useCallback } from 'react';
import { CFormInput, CFormSelect } from '@coreui/react-pro';
import type { AgentVendor } from '@/lib/types';
import {
  modelAfterVendorChange,
  pickerModels,
  vendorOptions,
} from '@/lib/agent-vendors';
import { useAgentRegistry } from '@/lib/use-agent-registry';

interface VendorModelPickerProps {
  vendor: AgentVendor;
  model: string;
  onVendorChange: (vendor: AgentVendor) => void;
  onModelChange: (model: string) => void;
  /** Label shown above the model combobox. Defaults to "Model". */
  modelLabel?: string;
  /** Placeholder shown when the model input is empty. */
  modelPlaceholder?: string;
  /** Optional name attributes so the picker can be embedded in a form. */
  vendorName?: string;
  modelName?: string;
  /** Disable both comboboxes. */
  disabled?: boolean;
}

export function VendorModelPicker({
  vendor,
  model,
  onVendorChange,
  onModelChange,
  modelLabel,
  modelPlaceholder,
  vendorName = 'agent_vendor',
  modelName = 'claude_model',
  disabled,
}: VendorModelPickerProps) {
  const registry = useAgentRegistry();
  const modelList = pickerModels(registry, vendor);
  const datalistId = `${modelName}-${vendor}-datalist`;

  const handleVendorChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const next = e.target.value as AgentVendor;
      onVendorChange(next);
      const nextModel = modelAfterVendorChange(registry, next, model);
      if (nextModel !== model) {
        onModelChange(nextModel);
      }
    },
    [registry, model, onVendorChange, onModelChange],
  );

  return (
    <div className="row g-2">
      <div className="col-md-5">
        <label className="form-label mb-1">Vendor</label>
        <CFormSelect
          name={vendorName}
          value={vendor}
          onChange={handleVendorChange}
          disabled={disabled}
        >
          {vendorOptions(registry, vendor).map((v) => (
            <option key={v.id} value={v.id}>
              {v.label}
            </option>
          ))}
        </CFormSelect>
      </div>
      <div className="col-md-7">
        <label className="form-label mb-1">{modelLabel ?? 'Model'}</label>
        <CFormInput
          name={modelName}
          value={model}
          onChange={(e) => onModelChange(e.target.value)}
          list={datalistId}
          placeholder={modelPlaceholder ?? 'Type or select a model…'}
          autoComplete="off"
          disabled={disabled}
        />
        <datalist id={datalistId}>
          {modelList.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </datalist>
      </div>
    </div>
  );
}
