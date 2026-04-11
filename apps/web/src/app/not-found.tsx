export const dynamic = 'force-dynamic';

export default function NotFound() {
  return (
    <div className="p-4">
      <h2>404 — Page Not Found</h2>
      <p>The page you are looking for does not exist.</p>
      <a href="/">Go to Dashboard</a>
    </div>
  );
}
