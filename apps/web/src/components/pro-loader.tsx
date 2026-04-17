/**
 * Free-tier component stubs — renders nothing for all pro components.
 *
 * This file replaces ``pro-loader.tsx`` when ``scripts/strip-pro.sh``
 * runs. It provides the same exports so TypeScript doesn't break, but
 * every component renders null and ``PRO_NAV_ITEMS`` is empty so the
 * sidebar silently drops the Inbox + Webhooks entries.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
export const PatchesPanel = (_props: any) => null;
export const NightCyclePanel = (_props: any) => null;
export const MessagesPanel = (_props: any) => null;

export const PRO_NAV_ITEMS: { name: string; href: string; icon: string[] }[] = [];
