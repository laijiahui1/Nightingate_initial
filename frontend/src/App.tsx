export default function App() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white px-6 py-4">
        <h1 className="text-lg font-semibold tracking-tight">Nightingale</h1>
        <p className="text-sm text-slate-500">Care note collaboration workspace</p>
      </header>
      <main className="mx-auto max-w-3xl px-6 py-10">
        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="text-base font-medium">Foundation scaffold is up</h2>
          <p className="mt-2 text-sm leading-relaxed text-slate-600">
            This is the Nightingale scaffold: FastAPI + PostgreSQL (RLS) backend,
            deterministic mock LLM, and this React + Tailwind PWA. The Care Note
            vertical slice — Glance Top Card and the longitudinal timeline —
            lands here in M2.
          </p>
          <p className="mt-3 text-sm text-slate-500">
            API health:{' '}
            <a
              className="font-medium text-slate-700 underline decoration-slate-300 underline-offset-2"
              href="/api/healthz"
              target="_blank"
              rel="noreferrer"
            >
              /api/healthz
            </a>
          </p>
        </div>
      </main>
    </div>
  );
}
