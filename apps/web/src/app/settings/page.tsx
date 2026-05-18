import { query } from '@/lib/db';
import { tryDbPage } from '@/lib/db-page';
import type { Settings } from '@/lib/types';
import { SettingsForm } from '@/components/SettingsForm';
import { EnvSettingsForm } from '@/components/EnvSettingsForm';

export const dynamic = 'force-dynamic';

export default async function SettingsPage() {
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
      <SettingsForm settings={r.data} />
      <EnvSettingsForm />
    </>
  );
}
