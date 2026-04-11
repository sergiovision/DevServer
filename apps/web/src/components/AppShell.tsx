'use client';

import React, { useState } from 'react';
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
import { useTheme } from './ThemeProvider';

export function AppShell({ children }: { children: React.ReactNode }) {
  const { theme, toggleTheme } = useTheme();
  const [sidebarVisible, setSidebarVisible] = useState(false);

  return (
    <div className="app-shell d-flex">
      <Sidebar visible={sidebarVisible} onVisibleChange={setSidebarVisible} />
      <div className="wrapper d-flex flex-column min-vh-100 flex-grow-1">
        <CHeader position="sticky" className="mb-0 p-0">
          <CContainer fluid className="px-3">
            <button
              type="button"
              className="btn btn-link nav-link px-2 app-sidebar-toggler"
              onClick={() => setSidebarVisible((v) => !v)}
              aria-label="Toggle navigation"
              aria-expanded={sidebarVisible}
            >
              <CIcon icon={cilMenu} size="lg" />
            </button>
            <CHeaderBrand className="me-auto d-flex align-items-center gap-2">
              <span className="fs-5 fw-semibold">DevServer</span>
              <span className="badge bg-primary bg-opacity-10 text-primary fw-normal" style={{ fontSize: '0.7rem' }}>
                Dashboard
              </span>
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
          <CContainer lg className="py-4">
            {children}
          </CContainer>
        </div>
        <CFooter className="px-4">
          <div className="text-body-secondary small">
            {process.env.NEXT_PUBLIC_HOSTNAME}
            {process.env.NEXT_PUBLIC_USER && (
              <span className="ms-2 text-body-tertiary">({process.env.NEXT_PUBLIC_USER})</span>
            )}
          </div>
          <div className="text-body-secondary small">
            DevServer &copy; {new Date().getFullYear()}
          </div>
          <div className="ms-auto text-body-secondary small">
            v{process.env.NEXT_PUBLIC_VERSION}
          </div>
        </CFooter>
      </div>
    </div>
  );
}
