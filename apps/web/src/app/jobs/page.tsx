import { JobsView } from '@/components/JobsView';
import { SchedulesPanel } from '@/components/SchedulesPanel';

export const dynamic = 'force-dynamic';

export default function JobsPage() {
  return (
    <>
      <SchedulesPanel />
      <JobsView />
    </>
  );
}
