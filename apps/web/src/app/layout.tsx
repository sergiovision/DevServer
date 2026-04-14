import type { Metadata } from 'next';
import './globals.css';
import { AppShell } from '@/components/AppShell';
import { ThemeProvider } from '@/components/ThemeProvider';
import { NotificationProvider } from '@/components/NotificationProvider';

export function generateMetadata(): Metadata {
  const host = process.env.NEXT_PUBLIC_HOSTNAME || 'unknown';
  const user = process.env.NEXT_PUBLIC_USER || 'unknown';
  return {
    title: `Devserver on ${host}(${user}) - Dashboard`,
    description: 'Real-time dashboard for managing autonomous coding agents',
  };
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" data-coreui-theme="light">
      <body>
        <ThemeProvider>
          <NotificationProvider>
            <AppShell>{children}</AppShell>
          </NotificationProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
