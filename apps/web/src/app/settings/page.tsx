import { query } from '@/lib/db';
import type { Settings } from '@/lib/types';
import { SettingsForm } from '@/components/SettingsForm';

export const dynamic = 'force-dynamic';

export default async function SettingsPage() {
  let settings: Record<string, unknown> = {};

  try {
    const result = await query<Settings>('SELECT key, value FROM settings');
    for (const row of result.rows) {
      settings[row.key] = row.value;
    }
  } catch (err) {
    console.error('Failed to fetch settings:', err);
  }

  return (
    <>
      <h2 className="mb-4">Settings</h2>
      <SettingsForm settings={settings} />
    </>
  );
}
