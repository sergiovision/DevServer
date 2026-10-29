/**
 * Agent vendor + model registry, as published by the worker.
 *
 * The worker's `services/agent_backends.py` is the only place a model id is
 * written down — it is also what actually launches the vendor CLIs. The web
 * app reads that registry from `GET /internal/agent-registry` instead of
 * keeping a hand-copied list that would drift every time a vendor ships a
 * model.
 *
 * This module holds:
 *   - the registry types,
 *   - `fetchAgentRegistry()` for server code (route handlers, server pages),
 *   - pure helpers that every picker uses to read the registry.
 *
 * Client components load the registry through `useAgentRegistry()` in
 * `use-agent-registry.ts`. Every helper accepts `null` for "not loaded yet"
 * so components can render immediately and fill in once the fetch resolves.
 */

import { WORKER_URL } from './worker-url';

export interface RegistryModel {
  id: string;
  label: string;
  is_default: boolean;
  /** Still accepted when stored on a task, but no longer offered in pickers. */
  deprecated: boolean;
}

export interface RegistryVendor {
  label: string;
  default_model: string;
  models: RegistryModel[];
}

export interface AgentRegistry {
  vendors: Record<string, RegistryVendor>;
  default_vendor: string;
  task_types: string[];
  repo_required_types: string[];
  git_flows: string[];
  billing_modes: string[];
  modes: string[];
}

/** One entry of a vendor `<select>`. */
export interface VendorOption {
  id: string;
  label: string;
}

// ─── Server-side loading ────────────────────────────────────────────────────

/** The registry only changes when the worker is redeployed; a minute is plenty. */
const REGISTRY_CACHE_TTL_MS = 60_000;
const REGISTRY_FETCH_TIMEOUT_MS = 5_000;

let cachedRegistry: { registry: AgentRegistry; fetchedAt: number } | null = null;

/**
 * Load the registry from the worker, cached in memory for a minute.
 *
 * Throws when the worker is unreachable and nothing is cached yet; callers
 * decide whether that is fatal. A stale copy is preferred over an error so a
 * worker restart does not blank every model picker in the dashboard.
 */
export async function fetchAgentRegistry(): Promise<AgentRegistry> {
  const now = Date.now();
  if (cachedRegistry && now - cachedRegistry.fetchedAt < REGISTRY_CACHE_TTL_MS) {
    return cachedRegistry.registry;
  }

  try {
    const res = await fetch(`${WORKER_URL}/internal/agent-registry`, {
      cache: 'no-store',
      signal: AbortSignal.timeout(REGISTRY_FETCH_TIMEOUT_MS),
    });
    if (!res.ok) {
      throw new Error(`worker returned ${res.status} for /internal/agent-registry`);
    }
    const registry = (await res.json()) as AgentRegistry;
    cachedRegistry = { registry, fetchedAt: now };
    return registry;
  } catch (err) {
    if (cachedRegistry) return cachedRegistry.registry;
    throw err;
  }
}

// ─── Pure helpers ───────────────────────────────────────────────────────────

function findVendor(registry: AgentRegistry | null, vendor: string): RegistryVendor | null {
  return registry?.vendors[vendor] ?? null;
}

/**
 * Vendors for a `<select>`. The currently selected vendor is always included,
 * so the control shows the stored value while the registry is still loading
 * (or if the worker no longer knows that vendor).
 */
export function vendorOptions(registry: AgentRegistry | null, selected: string): VendorOption[] {
  const options = Object.entries(registry?.vendors ?? {}).map(([id, entry]) => ({
    id,
    label: entry.label,
  }));
  const selectedIsListed = options.some((option) => option.id === selected);
  if (selected && !selectedIsListed) {
    options.unshift({ id: selected, label: selected });
  }
  return options;
}

/** Models a picker should suggest: current ones only, default first. */
export function pickerModels(registry: AgentRegistry | null, vendor: string): RegistryModel[] {
  const models = findVendor(registry, vendor)?.models ?? [];
  const current = models.filter((model) => !model.deprecated);
  const defaults = current.filter((model) => model.is_default);
  const others = current.filter((model) => !model.is_default);
  return [...defaults, ...others];
}

/** The vendor's default model id, or '' when unknown / not loaded yet. */
export function defaultModelFor(registry: AgentRegistry | null, vendor: string): string {
  return findVendor(registry, vendor)?.default_model ?? '';
}

/** Human-readable vendor name, falling back to the raw id. */
export function vendorLabel(registry: AgentRegistry | null, vendor: string): string {
  return findVendor(registry, vendor)?.label ?? vendor;
}

/** True when `model` is registered under `vendor` (deprecated ids included). */
export function isModelOfVendor(
  registry: AgentRegistry | null,
  vendor: string,
  model: string,
): boolean {
  const models = findVendor(registry, vendor)?.models ?? [];
  return models.some((entry) => entry.id === model);
}

/**
 * The model to keep after the operator switches vendor: the current one if
 * the new vendor also offers it, otherwise the new vendor's default.
 */
export function modelAfterVendorChange(
  registry: AgentRegistry | null,
  nextVendor: string,
  currentModel: string,
): string {
  if (isModelOfVendor(registry, nextVendor, currentModel)) {
    return currentModel;
  }
  return defaultModelFor(registry, nextVendor);
}
