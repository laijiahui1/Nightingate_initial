// Shared UI helpers for the Nightingale SPA: date formatting and role badges.
// Kept in one small module so every panel renders dates and role pills the
// same way.

const TYPE_STYLES: Record<string, string> = {
  system: 'bg-violet-50 text-violet-700 ring-violet-200',
  patient: 'bg-sky-50 text-sky-700 ring-sky-200',
  staff: 'bg-slate-100 text-slate-700 ring-slate-200',
  clinician: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
};

export const ROLE_LABEL: Record<string, string> = {
  system: 'AI',
  patient: 'Patient',
  staff: 'Staff',
  clinician: 'Clinician',
};

export function roleBadge(role: string | null | undefined): string {
  const key = role ?? 'system';
  return TYPE_STYLES[key] ?? TYPE_STYLES.system;
}

export function roleLabel(role: string | null | undefined): string {
  const key = role ?? 'system';
  return ROLE_LABEL[key] ?? key;
}

export function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
