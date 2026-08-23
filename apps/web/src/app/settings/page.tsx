import Link from 'next/link';
import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import type { Settings } from '@/lib/types';
import { SettingsForm } from '@/components/SettingsForm';
import { EnvSettingsForm } from '@/components/EnvSettingsForm';
import { DatabaseSettingsCard } from '@/components/DatabaseConfigFields';
import { LicensePanel } from '@/components/pro-loader';

export const dynamic = 'force-dynamic';

export default async function SettingsPage() {
  // Read at request time (not build) so the Docker runtime DEPLOY_MODE=docker
  // is honoured — this picks the 2-option (host) vs 3-option (docker) DB UI.
  const deployMode =
    process.env.DEPLOY_MODE ||
    (process.env.NODE_ENV === 'production' ? 'production' : 'development');

  const r = await tryDbPage(async () => {
    const result = await query<Settings>('SELECT key, value FROM settings');
    const settings: Record<string, unknown> = {};
    for (const row of result.rows) {
      settings[row.key] = row.value;
    }
    return settings;
  });

  if (!r.ok) return r.panel;

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h2 className="mb-0">Settings</h2>
        {/* The wizard is only auto-shown until the setup cookie is set, so on
            a fresh install reached through a different browser — or after
            clearing cookies — there was no way back to it from the UI. The
            caption rides with the button so the hint sits beside what it
            describes; it drops out below sm, where the row would wrap. */}
        <div className="d-flex align-items-center gap-3">
          <small className="text-body-secondary text-end d-none d-sm-block">
            Running DevServer for the first time? Start with Setup.
          </small>
          <Link href="/setup" className="btn btn-primary flex-shrink-0">
            Setup
          </Link>
        </div>
      </div>
      <LicensePanel />
      <SettingsForm settings={r.data} />
      <DatabaseSettingsCard deployMode={deployMode} />
      <EnvSettingsForm />
    </>
  );
}
