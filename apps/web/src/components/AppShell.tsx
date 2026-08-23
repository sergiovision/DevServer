'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';
import {
  CContainer,
  CHeader,
  CHeaderBrand,
  CHeaderNav,
  CNavItem,
  CFooter,
} from '@coreui/react-pro';
import CIcon from '@coreui/icons-react';
import { cilSun, cilMoon, cilMenu } from '@coreui/icons';
import { Sidebar } from './Sidebar';
import { AskAgentLauncher } from './AskAgentLauncher';
import { useTheme } from './ThemeProvider';
import { getLicenseStatus } from '@/lib/license-client';

const COLLAPSE_KEY = 'devserver.sidebar.collapsed';

/**
 * Routes that want the whole viewport: the sidebar drops to an icons-only
 * rail and the content container loses most of its padding. The session
 * observatory is a two-pane live view where every pixel of width is
 * transcript.
 */
const DENSE_ROUTES = ['/pro/sessions'];

export function AppShell({ children }: { children: React.ReactNode }) {
  const { theme, toggleTheme } = useTheme();
  const [sidebarVisible, setSidebarVisible] = useState(false);
  // Starts false so SSR and the first client render agree; the stored
  // preference is applied in an effect right after hydration.
  const [collapsed, setCollapsed] = useState(false);
  // Effective licensing tier ('pro' | 'free'). Defaults to 'free' — the
  // /api/pro/license route does not exist in the free build (404) and a
  // worker outage must not break the shell, so any failure keeps 'free'.
  // getLicenseStatus resolves instantly from the session cache on reloads
  // (no network) and only fetches when the cache is cold, deduped against
  // any concurrent caller. Initial state stays 'free' so SSR and client
  // hydration render the same markup.
  const [licenseType, setLicenseType] = useState('free');
  const pathname = usePathname();

  useEffect(() => {
    let cancelled = false;
    getLicenseStatus().then((data) => {
      if (!cancelled && typeof data?.plan === 'string' && data.plan) {
        setLicenseType(data.plan);
      }
    });
    return () => { cancelled = true; };
  }, []);

  const dense = DENSE_ROUTES.some((r) => pathname.startsWith(r));

  // A dense route forces the rail collapsed; anywhere else the operator's
  // stored preference wins. Reading storage on the way out rather than
  // restoring a value captured on the way in matters: both would otherwise
  // run in the same commit on a cold load and the capture would see the
  // pre-hydration default instead of the real preference.
  //
  // Depends on `dense` alone, so a manual toggle made while on a dense route
  // sticks — it only re-runs when you enter or leave one.
  useEffect(() => {
    if (dense) {
      setCollapsed(true);
      return;
    }
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === '1');
    } catch {
      setCollapsed(false); // storage unavailable — expanded is the default
    }
  }, [dense]);

  // One button, two jobs: the sidebar is a rail on desktop and a drawer on
  // mobile, matching the breakpoint the CSS uses. Manual toggles persist;
  // the automatic dense-route collapse above does not.
  const handleToggle = useCallback(() => {
    if (typeof window !== 'undefined' && window.innerWidth < 768) {
      setSidebarVisible((v) => !v);
      return;
    }
    setCollapsed((v) => {
      try {
        localStorage.setItem(COLLAPSE_KEY, v ? '0' : '1');
      } catch {
        /* storage unavailable — the toggle still works for this session */
      }
      return !v;
    });
  }, []);

  // Setup wizard gets a clean full-screen layout — no sidebar, header, or footer.
  if (pathname === '/setup') {
    return <>{children}</>;
  }

  return (
    <div className="app-shell d-flex">
      <Sidebar
        visible={sidebarVisible}
        onVisibleChange={setSidebarVisible}
        collapsed={collapsed}
      />
      <div className="wrapper d-flex flex-column min-vh-100 flex-grow-1">
        <CHeader position="sticky" className="mb-0 p-0">
          <CContainer fluid className="px-3">
            <button
              type="button"
              className="btn btn-link nav-link px-2 app-sidebar-toggler"
              onClick={handleToggle}
              aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
              title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
              aria-expanded={!collapsed}
            >
              <CIcon icon={cilMenu} size="lg" />
            </button>
            <CHeaderBrand
              className="me-auto d-flex align-items-center gap-2"
              style={{ minWidth: 0 }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/icon.png" alt="" height={28} width={28} className="brand-mark flex-shrink-0" />
              <span className="fs-5 fw-semibold flex-shrink-0">DevServer</span>
              {/* Decorative — always says the same thing. Dropped on the
                  narrowest screens so the host below keeps its room. */}
              <span
                className="badge bg-primary bg-opacity-10 text-primary fw-normal d-none d-sm-inline"
                style={{ fontSize: '0.7rem' }}
              >
                Dashboard
              </span>
              {/* Which machine you are looking at — same values the footer
                  shows, but up here it stays visible on mobile, where the
                  footer is a scroll away. Truncates rather than pushing the
                  header wider on a narrow screen. */}
              {process.env.NEXT_PUBLIC_HOSTNAME && (
                <span
                  className="text-body-secondary fw-normal text-truncate"
                  style={{ fontSize: '0.78rem', minWidth: 0 }}
                  title={`Host this dashboard runs on: ${process.env.NEXT_PUBLIC_HOSTNAME}`}
                >
                  {process.env.NEXT_PUBLIC_HOSTNAME}
                  {process.env.NEXT_PUBLIC_USER && (
                    <span className="text-body-tertiary">
                      ({process.env.NEXT_PUBLIC_USER})
                    </span>
                  )}
                </span>
              )}
            </CHeaderBrand>
            <CHeaderNav>
              <CNavItem>
                <button
                  className="btn btn-link nav-link px-2"
                  onClick={toggleTheme}
                  title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
                  aria-label="Toggle theme"
                >
                  <CIcon icon={theme === 'light' ? cilMoon : cilSun} size="lg" />
                </button>
              </CNavItem>
            </CHeaderNav>
          </CContainer>
        </CHeader>
        <div className="body flex-grow-1">
          <CContainer fluid className={dense ? 'py-2 px-2' : 'py-4 px-4'}>
            {children}
          </CContainer>
        </div>
        <CFooter className="px-4">
          {/* Host and user moved to the header brand — they are the same
              values and duplicating them here bought nothing. */}
          <div className="text-body-secondary small">
            v{process.env.NEXT_PUBLIC_VERSION}{' '}
            <span className="text-body-tertiary">
              {process.env.NEXT_PUBLIC_EDITION || 'pro'}(build) {licenseType}(license)
            </span>
          </div>
          <div className="ms-auto text-body-secondary small">
            DevServer &copy; {new Date().getFullYear()}
          </div>
        </CFooter>
      </div>
      {/* Present on every dashboard page. The `/setup` early return above keeps
          it off the wizard without a second condition. */}
      <AskAgentLauncher />
    </div>
  );
}
