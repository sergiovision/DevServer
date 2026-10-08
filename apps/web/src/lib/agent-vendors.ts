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
    // Current Claude line (2026-10-08): Sonnet 5.5, Opus 5.5, Fable 5.1.
    // `claude-mythos-5` is deliberately absent — Project Glasswing only.
    label: 'Anthropic',
    models: [
      { id: 'claude-sonnet-5-5',            label: 'Claude Sonnet 5.5 (default, Max)' },
      { id: 'claude-opus-5-5',              label: 'Claude Opus 5.5 (flagship — agentic coding, long-horizon)' },
      { id: 'claude-fable-5-1',             label: 'Claude Fable 5.1 (most capable, premium)' },
      { id: 'claude-sonnet-5',              label: 'Claude Sonnet 5' },
      { id: 'claude-opus-5',                label: 'Claude Opus 5' },
      { id: 'claude-fable-5',               label: 'Claude Fable 5' },
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
    // Antigravity CLI (`agy`). As of agy 1.1.2 `--model` is validated strictly
    // and the reasoning effort is baked into the slug (`<model>-<low|high>`);
    // the old bare slugs are rejected. Every slug below is listed by
    // `agy models` on agy 1.2.16 (Gemini 3.5 Flash is gone). Keep in sync with
    // VENDOR_MODELS in apps/worker/src/services/agent_backends.py.
    models: [
      { id: 'gemini-3.1-pro-high',     label: 'Gemini 3.1 Pro (High) — strong coding, default' },
      { id: 'gemini-3.1-pro-low',      label: 'Gemini 3.1 Pro (Low) — faster' },
      { id: 'gemini-3.8-flash-high',   label: 'Gemini 3.8 Flash (High) — newest flash, deeper reasoning' },
      { id: 'gemini-3.8-flash-medium', label: 'Gemini 3.8 Flash (Medium) — newest flash, balanced' },
      { id: 'gemini-3.8-flash-low',    label: 'Gemini 3.8 Flash (Low) — newest flash, fastest' },
      { id: 'gemini-3.7-flash-high',   label: 'Gemini 3.7 Flash (High)' },
      { id: 'gemini-3.7-flash-low',    label: 'Gemini 3.7 Flash (Low) — fast, cheap' },
      { id: 'gemini-3.6-flash-high',   label: 'Gemini 3.6 Flash (High)' },
      { id: 'gemini-3.6-flash-medium', label: 'Gemini 3.6 Flash (Medium)' },
      { id: 'gemini-3.6-flash-low',    label: 'Gemini 3.6 Flash (Low)' },
    ],
  },
  {
    id: 'openai',
    label: 'OpenAI',
    // Slugs from the model catalog bundled in codex-cli 0.155.1. GPT-6 Astra
    // is the new flagship; the GPT-5.6 trio (Sol / Terra / Luna) remains the
    // agentic-coding line. `gpt-5.5-codex` was never a real slug.
    models: [
      { id: 'gpt-6-astra',   label: 'GPT-6 Astra — most capable, complex demanding work' },
      { id: 'gpt-5.6-sol',   label: 'GPT-5.6 Sol — frontier agentic coding' },
      { id: 'gpt-5.6-terra', label: 'GPT-5.6 Terra — balanced agentic coding' },
      { id: 'gpt-5.6-luna',  label: 'GPT-5.6 Luna — fast + affordable agentic coding' },
      { id: 'gpt-5.5',       label: 'GPT-5.5 (frontier: complex coding + research)' },
      { id: 'gpt-5.4',       label: 'GPT-5.4 (strong everyday coding)' },
      { id: 'gpt-5.3-codex', label: 'GPT-5.3 Codex (heavy reasoning, agentic)' },
      { id: 'gpt-5.4-mini',  label: 'GPT-5.4 Mini (small, fast, cost-efficient)' },
      { id: 'gpt-5.2',       label: 'GPT-5.2 (long-running agents)' },
    ],
  },
  {
    id: 'glm',
    label: 'GLM (Zhipu)',
    // Verified against GET https://open.bigmodel.cn/api/paas/v4/models (2026-08-19).
    // glm-5.3 (2026-08-14) — same 744B-A40B MoE base as 5.2, far heavier
    // post-training: ~50% better coding, top open-weights on Terminal Bench 3.0.
    // Runs through the Claude Code CLI like the rest of GLMBackend.
    models: [
      { id: 'glm-5.3',       label: 'GLM-5.3 (thinking, latest flagship — best open-weights coding)' },
      { id: 'glm-5.2',       label: 'GLM-5.2 (thinking, previous flagship)' },
      { id: 'glm-5.1',       label: 'GLM-5.1 (thinking, SWE-bench Pro leader, 8x cheaper)' },
      { id: 'glm-5-turbo',   label: 'GLM-5 Turbo (fast, cheap)' },
      { id: 'glm-5',         label: 'GLM-5' },
      { id: 'glm-4.7',       label: 'GLM-4.7 (previous generation)' },
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
