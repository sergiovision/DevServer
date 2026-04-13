'use client';

import React, { useEffect } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import CIcon from '@coreui/icons-react';
import {
  cilSpeedometer,
  cilTask,
  cilStorage,
  cilSettings,
  cilCode,
  cilClock,
  cilListRich,
  cilDescription,
} from '@coreui/icons';

interface SidebarProps {
  visible: boolean;
  onVisibleChange: (visible: boolean) => void;
}

const navItems = [
  { name: 'Dashboard', href: '/', icon: cilSpeedometer },
  { name: 'Tasks', href: '/tasks', icon: cilTask },
  { name: 'Ideas', href: '/ideas', icon: cilListRich },
  { name: 'Jobs', href: '/jobs', icon: cilClock },
  { name: 'Repos', href: '/repos', icon: cilStorage },
  { name: 'Settings', href: '/settings', icon: cilSettings },
  { name: 'Logs', href: '/logs', icon: cilDescription },
];

export function Sidebar({ visible, onVisibleChange }: SidebarProps) {
  const pathname = usePathname();

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
        <div className="app-sidebar-header border-bottom d-flex align-items-center gap-2 px-3">
          <CIcon icon={cilCode} height={28} className="text-primary" />
          <span className="fs-5 fw-bold sidebar-brand-text">DevServer</span>
        </div>
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
