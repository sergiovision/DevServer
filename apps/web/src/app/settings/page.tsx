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
      <h2 className="mb-4">Settings</h2>
      <LicensePanel />
      <SettingsForm settings={r.data} />
      <DatabaseSettingsCard deployMode={deployMode} />
      <EnvSettingsForm />
    </>
  );
}
