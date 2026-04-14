import { query } from '@/lib/db';
import { redirect } from 'next/navigation';
import { cookies } from 'next/headers';
import { SetupWizard } from '@/components/SetupWizard';

export const dynamic = 'force-dynamic';

/** Set the long-lived cookie and redirect to dashboard. */
async function markDoneAndRedirect() {
  const jar = await cookies();
  jar.set('devserver_setup', '1', {
    path: '/',
    maxAge: 60 * 60 * 24 * 365 * 10,
    httpOnly: false,
    sameSite: 'lax',
  });
  redirect('/');
}

export default async function SetupPage({
  searchParams,
}: {
  searchParams?: Promise<{ force?: string }>;
}) {
  const sp = (await searchParams) ?? {};
  const force = sp.force === '1' || sp.force === 'true';
  if (force) {
    return <SetupWizard />;
  }
  try {
    // 1. Explicit flag — set by the wizard or "skip" link.
    const flagResult = await query(
      "SELECT value FROM settings WHERE key = 'setup_completed'",
    );
    if (flagResult.rows.length > 0) {
      const val = flagResult.rows[0].value;
      if (val === true || val === 'true' || val === '"true"') {
        await markDoneAndRedirect();
      }
    }

    // 2. Existing installation heuristic — if tasks already exist in the DB
    //    this is clearly not a first boot.  Auto-complete setup silently.
    const tasksResult = await query('SELECT 1 FROM tasks LIMIT 1');
    if (tasksResult.rows.length > 0) {
      // Persist the flag so we don't re-check next time.
      await query(
        `INSERT INTO settings (key, value)
         VALUES ('setup_completed', 'true')
         ON CONFLICT (key) DO UPDATE SET value = 'true'`,
      );
      await markDoneAndRedirect();
    }
  } catch {
    // DB might not be reachable on very first boot — show the wizard anyway.
  }

  return <SetupWizard />;
}
