'use client';

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import CIcon from '@coreui/icons-react';
import {
  cilSpeedometer,
  cilTask,
  cilStorage,
  cilSettings,
  cilClock,
  cilListRich,
  cilDescription,
  cilCopy,
} from '@coreui/icons';
import { PRO_NAV_ITEMS } from './pro-loader';

interface SidebarProps {
  visible: boolean;
  onVisibleChange: (visible: boolean) => void;
}

// Free nav entries. Pro-only entries (Webhooks) are spliced in
// from ``PRO_NAV_ITEMS`` so ``strip-pro.sh`` removes them along with
// the rest of the pro bundle.
const baseNavItems = [
  { name: 'Dashboard', href: '/', icon: cilSpeedometer },
  { name: 'Tasks', href: '/tasks', icon: cilTask },
  { name: 'Templates', href: '/templates', icon: cilCopy },
  { name: 'Ideas', href: '/ideas', icon: cilListRich },
  { name: 'Jobs', href: '/jobs', icon: cilClock },
  { name: 'Repos', href: '/repos', icon: cilStorage },
  { name: 'Settings', href: '/settings', icon: cilSettings },
  { name: 'Logs', href: '/logs', icon: cilDescription },
];

// Webhooks sits next to Templates in pro; in free it's absent.
const navItems = [
  ...baseNavItems.slice(0, 3),       // Dashboard, Tasks, Templates
  ...PRO_NAV_ITEMS,                  // Webhooks (pro)
  ...baseNavItems.slice(3),          // Ideas, Jobs, Repos, Settings, Logs
];

export function Sidebar({ visible, onVisibleChange }: SidebarProps) {
  const pathname = usePathname();
  // "Needs you" pill: tasks awaiting a human decision (blocked = plan
  // approval / budget / preflight). Polled so the sidebar is a pull surface.
  const [needsYou, setNeedsYou] = useState(0);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const res = await fetch('/api/tasks?status=blocked&limit=200');
        if (res.ok && active) {
          const rows = await res.json();
          setNeedsYou(Array.isArray(rows) ? rows.length : 0);
        }
      } catch {
        // leave last-known count on transient failure
      }
    };
    load();
    const id = setInterval(load, 15000);
    return () => { active = false; clearInterval(id); };
  }, []);

  // Close drawer on Escape when open on mobile
  useEffect(() => {
    if (!visible) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onVisibleChange(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [visible, onVisibleChange]);

  const handleLinkClick = () => {
    // Close drawer on mobile when a link is clicked
    if (typeof window !== 'undefined' && window.innerWidth < 768) {
      onVisibleChange(false);
    }
  };

  return (
    <>
      <div
        className={`app-sidebar-backdrop${visible ? ' show' : ''}`}
        onClick={() => onVisibleChange(false)}
        aria-hidden="true"
      />
      <aside
        className={`app-sidebar border-end${visible ? ' show' : ''}`}
        aria-label="Main navigation"
      >
        <Link
          href="/"
          className="app-sidebar-header border-bottom d-flex align-items-center justify-content-center px-3 py-4 text-decoration-none"
          onClick={handleLinkClick}
          aria-label="DevServer home"
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/devserver-logo.png"
            alt="DevServer"
            width={120}
            className="brand-mark sidebar-brand-logo"
            style={{ height: 'auto' }}
          />
        </Link>
        <nav className="app-sidebar-nav">
          <div className="app-sidebar-nav-title">Navigation</div>
          <ul className="app-sidebar-nav-list">
            {navItems.map((item) => {
              const isActive =
                item.href === '/'
                  ? pathname === '/'
                  : pathname.startsWith(item.href);
              return (
                <li key={item.href} className="app-sidebar-nav-item">
                  <Link
                    href={item.href}
                    className={`app-sidebar-nav-link${isActive ? ' active' : ''}`}
                    onClick={handleLinkClick}
                    aria-current={isActive ? 'page' : undefined}
                  >
                    <CIcon customClassName="app-sidebar-nav-icon" icon={item.icon} />
                    <span>{item.name}</span>
                    {item.name === 'Tasks' && needsYou > 0 && (
                      <span
                        className="badge rounded-pill bg-warning text-dark ms-auto"
                        title={`${needsYou} task(s) awaiting your action`}
                      >
                        {needsYou}
                      </span>
                    )}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <div className="app-sidebar-footer border-top" />
      </aside>
    </>
  );
}
