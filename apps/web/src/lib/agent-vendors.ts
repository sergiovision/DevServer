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
      { id: 'claude-sonnet-5',              label: 'Claude Sonnet 5 (default, Max)' },
      { id: 'claude-fable-5',               label: 'Claude Fable 5 (most capable, premium)' },
      { id: 'claude-opus-4-8',              label: 'Claude Opus 4.8' },
      { id: 'claude-opus-4-7',              label: 'Claude Opus 4.7' },
      { id: 'claude-opus-4-6',              label: 'Claude Opus 4.6' },
      { id: 'claude-sonnet-4-6',            label: 'Claude Sonnet 4.6' },
      { id: 'claude-haiku-4-5-20251001',    label: 'Claude Haiku 4.5' },
      { id: 'claude-opus-4-5',              label: 'Claude Opus 4.5' },
      { id: 'claude-sonnet-4-5',            label: 'Claude Sonnet 4.5' },
    ],
  },
  {
    id: 'google',
    label: 'Google (Antigravity)',
    // Google retired the Gemini CLI (2026-06-18); the worker now drives the
    // Antigravity CLI (`agy`). Slugs verified against agy 1.0.10.
    models: [
      { id: 'gemini-3.5-pro',   label: 'Gemini 3.5 Pro (frontier intelligence + action, default)' },
      { id: 'gemini-3.1-pro',   label: 'Gemini 3.1 Pro (strong coding)' },
      { id: 'gemini-3.5-flash', label: 'Gemini 3.5 Flash (fast, cheap)' },
    ],
  },
  {
    id: 'openai',
    label: 'OpenAI',
    models: [
      { id: 'gpt-5.5-codex', label: 'GPT-5.5 Codex (latest frontier, 1M ctx)' },
      { id: 'gpt-5.4',       label: 'GPT-5.4 (reasoning + coding, integrates Codex)' },
      { id: 'gpt-5.3-codex', label: 'GPT-5.3 Codex (heavy reasoning, agentic)' },
      { id: 'gpt-5.4-mini',  label: 'GPT-5.4 Mini (Azure Foundry test)' },
      { id: 'gpt-5.2',       label: 'GPT-5.2 (reasoning)' },
      { id: 'o4-mini',       label: 'o4-mini (cheap reasoning)' },
    ],
  },
  {
    id: 'glm',
    label: 'GLM (Zhipu)',
    models: [
      { id: 'glm-5.2',       label: 'GLM-5.2 (thinking, latest flagship)' },
      { id: 'glm-5.1',       label: 'GLM-5.1 (thinking, SWE-bench Pro leader, 8x cheaper)' },
      { id: 'glm-5',         label: 'GLM-5' },
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

/**
 * Friendly vendor name for the "Filling with {Vendor}…" button caption —
 * e.g. "Filling with Claude…" / "Filling with Google…". Reflects which
 * System LLM vendor the Fill Task command runs through.
 */
export function fillVendorLabel(vendor: AgentVendor): string {
  switch (vendor) {
    case 'anthropic':
      return 'Claude';
    case 'google':
      return 'Google';
    case 'openai':
      return 'OpenAI';
    case 'glm':
      return 'GLM';
    default:
      return vendor;
  }
}
