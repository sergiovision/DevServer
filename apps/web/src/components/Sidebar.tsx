'use client';

/**
 * Main navigation: the page list (shared with the command palette via
 * `lib/nav-items.ts`) and the Needs You pill on Tasks.
 *
 * The pill reads the tab-wide shared poll (`lib/needs-you.ts`) instead of
 * running its own interval, so it costs one request per tick no matter how
 * many surfaces show the count.
 */

import React, { useEffect } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import CIcon from '@coreui/icons-react';
import { CBadge } from '@coreui/react-pro';
import { NAV_ITEMS, isNavItemActive } from '@/lib/nav-items';
import { needsYouHref, totalNeedsYou, useNeedsYou } from '@/lib/needs-you';

interface SidebarProps {
  visible: boolean;
  onVisibleChange: (visible: boolean) => void;
  /**
   * Icons-only rail. Desktop only — on mobile the sidebar is an off-canvas
   * drawer where a 56px rail would be useless, so the CSS ignores it there.
   */
  collapsed?: boolean;
}

export function Sidebar({ visible, onVisibleChange, collapsed = false }: SidebarProps) {
  const pathname = usePathname();
  const router = useRouter();
  const needsYouCounts = useNeedsYou();
  const needsYou = totalNeedsYou(needsYouCounts);
  const needsYouTitle = needsYouCounts
    ? `${needsYouCounts.blocked} blocked, ${needsYouCounts.plans_awaiting_decision} plan(s) awaiting your decision`
    : '';

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
        className={`app-sidebar border-end${visible ? ' show' : ''}${
          collapsed ? ' collapsed' : ''
        }`}
        aria-label="Main navigation"
      >
        <Link
          href="/"
          className="app-sidebar-header border-bottom d-flex align-items-center justify-content-center px-3 py-4 text-decoration-none"
          onClick={handleLinkClick}
          aria-label="DevServer home"
        >
          {/* The wordmark does not survive a 56px rail — swap in the square
              icon so the header keeps an anchor back to the dashboard. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={collapsed ? '/icon.png' : '/devserver-logo.png'}
            alt="DevServer"
            width={collapsed ? 28 : 120}
            className="brand-mark sidebar-brand-logo"
            style={{ height: 'auto' }}
          />
        </Link>
        <nav className="app-sidebar-nav">
          <div className="app-sidebar-nav-title">Navigation</div>
          <ul className="app-sidebar-nav-list">
            {NAV_ITEMS.map((item) => {
              const isActive = isNavItemActive(item.href, pathname);
              return (
                <li key={item.href} className="app-sidebar-nav-item">
                  <Link
                    href={item.href}
                    className={`app-sidebar-nav-link${isActive ? ' active' : ''}`}
                    onClick={handleLinkClick}
                    aria-current={isActive ? 'page' : undefined}
                    // The only affordance left once the label is hidden.
                    title={collapsed ? item.name : undefined}
                  >
                    <CIcon customClassName="app-sidebar-nav-icon" icon={item.icon} />
                    <span className="app-sidebar-nav-label">{item.name}</span>
                    {item.href === '/tasks' && needsYou > 0 && (
                      // A badge inside the Tasks link, not a nested <a>:
                      // clicking it goes to the filtered list instead.
                      <CBadge
                        color="warning"
                        shape="rounded-pill"
                        textColor="dark"
                        className="ms-auto app-sidebar-nav-badge"
                        title={`${needsYouTitle} — show them`}
                        aria-label={`${needsYou} task(s) need you: ${needsYouTitle}`}
                        onClick={(event: React.MouseEvent) => {
                          event.preventDefault();
                          event.stopPropagation();
                          handleLinkClick();
                          router.push(needsYouHref(needsYouCounts));
                        }}
                      >
                        {needsYou}
                      </CBadge>
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
