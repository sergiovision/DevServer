/**
 * Free-tier component stubs — renders nothing for all pro components.
 *
 * This file replaces ``pro-loader.tsx`` when ``scripts/strip-pro.sh``
 * runs. It provides the same exports so TypeScript doesn't break, but
 * every component renders null.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
export const PatchesPanel = (_props: any) => null;
export const NightCyclePanel = (_props: any) => null;
