/**
 * The dashboard's top-level pages, in sidebar order.
 *
 * Why it exists: the sidebar and the command palette both list "every page".
 * Two hand-maintained lists drift — a page added to the sidebar would silently
 * be missing from ⌘K. One exported constant keeps them identical.
 *
 * Invariant: `href` values are unique (React keys and palette entry ids rely
 * on it) and `/` is the only exact-match route; every other entry is active
 * for its whole subtree.
 */

import {
  cilSpeedometer,
  cilTask,
  cilStorage,
  cilSettings,
  cilClock,
  cilListRich,
  cilDescription,
  cilCopy,
  cilBolt,
  cilTerminal,
  cilShareBoxed,
} from '@coreui/icons';

/** CoreUI icons are SVG path tuples; `CIcon` accepts them as `icon`. */
export type NavIcon = typeof cilSpeedometer;

export interface NavItem {
  name: string;
  href: string;
  icon: NavIcon;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { name: 'Dashboard', href: '/', icon: cilSpeedometer },
  { name: 'Tasks', href: '/tasks', icon: cilTask },
  { name: 'Templates', href: '/templates', icon: cilCopy },
  { name: 'Webhooks', href: '/webhooks', icon: cilBolt },
  { name: 'Ideas', href: '/ideas', icon: cilListRich },
  { name: 'Jobs', href: '/jobs', icon: cilClock },
  { name: 'Repos', href: '/repos', icon: cilStorage },
  { name: 'Settings', href: '/settings', icon: cilSettings },
  { name: 'Agent Peers', href: '/a2a', icon: cilShareBoxed },
  { name: 'Sessions', href: '/sessions', icon: cilTerminal },
  { name: 'Logs', href: '/logs', icon: cilDescription },
];

/** True when `pathname` is inside the page `href` (exact for the dashboard). */
export function isNavItemActive(href: string, pathname: string | null): boolean {
  if (!pathname) return false;
  if (href === '/') return pathname === '/';
  return pathname === href || pathname.startsWith(`${href}/`);
}
