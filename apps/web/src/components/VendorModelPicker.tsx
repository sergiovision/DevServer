/**
 * VendorModelPicker — two linked comboboxes (vendor + model) for the
 * AgentBackend abstraction.
 *
 * Left combobox: hard select of the vendor (Anthropic / Google / OpenAI /
 * Qwen). Right combobox: free-text input with a datalist populated from
 * the selected vendor's models, so users can either pick a suggested
 * model or type a custom one (useful for pinned / dated model names like
 * ``claude-haiku-4-5-20251001``).
 *
 * Changing the vendor resets the model to the vendor's default model,
 * unless the current model string already belongs to the new vendor's
 * list (in that case we leave it alone).
 */
'use client';

import React, { useCallback } from 'react';
import { CFormInput, CFormSelect } from '@coreui/react-pro';
import type { AgentVendor } from '@/lib/types';
import {
  AGENT_VENDORS,
  defaultModelForVendor,
  modelsForVendor,
} from '@/lib/agent-vendors';

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
  const modelList = modelsForVendor(vendor);
  const datalistId = `${modelName}-${vendor}-datalist`;

  const handleVendorChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const next = e.target.value as AgentVendor;
      onVendorChange(next);
      // If the current model string doesn't belong to the new vendor's
      // suggested list, reset it to the new default. Preserves user input
      // if they typed a custom string that happens to be valid for both.
      const belongsToNextVendor = modelsForVendor(next).some((m) => m.id === model);
      if (!belongsToNextVendor) {
        onModelChange(defaultModelForVendor(next));
      }
    },
    [model, onVendorChange, onModelChange],
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
          {AGENT_VENDORS.map((v) => (
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
