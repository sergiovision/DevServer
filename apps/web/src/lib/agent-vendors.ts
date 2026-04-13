/**
 * Vendor + model registry for the AgentBackend abstraction.
 *
 * Mirrors ``apps/worker/src/services/agent_backends.py`` — whenever the
 * Python VENDOR_MODELS list changes, update this file to match. The UI
 * reads this (via ``VendorModelPicker``) to populate the two-step
 * "vendor → model" combobox on the task form.
 *
 * We intentionally duplicate the list client-side instead of fetching it
 * from the worker: the dashboard renders before a WS round-trip and the
 * list is tiny. If the two sides ever drift, the Python side is the
 * authority (that's what actually runs the CLI).
 */

import type { AgentVendor } from './types';

export interface VendorModel {
  id: string;
  label: string;
}

export interface VendorEntry {
  id: AgentVendor;
  label: string;
  models: VendorModel[];
}

export const AGENT_VENDORS: VendorEntry[] = [
  {
    id: 'anthropic',
    label: 'Anthropic',
    models: [
      { id: 'claude-opus-4-6',              label: 'Claude Opus 4.6 (most capable)' },
      { id: 'claude-sonnet-4-6',            label: 'Claude Sonnet 4.6' },
      { id: 'claude-haiku-4-5-20251001',    label: 'Claude Haiku 4.5' },
      { id: 'claude-opus-4-5',              label: 'Claude Opus 4.5' },
      { id: 'claude-sonnet-4-5',            label: 'Claude Sonnet 4.5' },
    ],
  },
  {
    id: 'google',
    label: 'Google',
    models: [
      { id: 'gemini-3-pro-preview',   label: 'Gemini 3 Pro Preview (strong coding)' },
      { id: 'gemini-3-flash-preview', label: 'Gemini 3 Flash Preview (cheap, fast)' },
      { id: 'gemini-2.5-pro',         label: 'Gemini 2.5 Pro (stable)' },
      { id: 'gemini-pro-latest',      label: 'Gemini Pro (latest alias)' },
    ],
  },
  {
    id: 'openai',
    label: 'OpenAI',
    models: [
      { id: 'gpt-5.3-codex', label: 'GPT-5.3 Codex (coding-tuned)' },
      { id: 'gpt-5.2',       label: 'GPT-5.2 (reasoning)' },
      { id: 'o4-mini',       label: 'o4-mini (cheap reasoning)' },
    ],
  },
  {
    id: 'glm',
    label: 'GLM (Zhipu)',
    models: [
      { id: 'glm-5.1',       label: 'GLM-5.1 (thinking, SWE-bench Pro leader, 8x cheaper)' },
      { id: 'glm-5',         label: 'GLM-5' },
      { id: 'glm-4.7-flash', label: 'GLM-4.7 Flash (free)' },
      { id: 'glm-4.5-air',   label: 'GLM-4.5 Air (budget)' },
    ],
  },
];

/** Look up the models for a given vendor id. */
export function modelsForVendor(vendor: AgentVendor): VendorModel[] {
  const entry = AGENT_VENDORS.find((v) => v.id === vendor);
  return entry?.models ?? [];
}

/** Default model suggestion for a vendor (first in the list). */
export function defaultModelForVendor(vendor: AgentVendor): string {
  return modelsForVendor(vendor)[0]?.id ?? '';
}
