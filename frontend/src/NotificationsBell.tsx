// Unread-mention notification bell for staff/clinician/admin. Shows a count
// badge, and a dropdown listing unread mentions with per-item "Mark read".
// Refreshes on mount, on role switch (token change), on an interval while
// mounted, and every time the dropdown is opened.

import { useCallback, useEffect, useRef, useState } from 'react';
import { api, messageOf, type MentionSummary, type Role } from './api';
import { formatDate, roleLabel } from './ui';

interface NotificationsBellProps {
  token: string;
  role: Role;
}

export function NotificationsBell({ token, role }: NotificationsBellProps) {
  const [open, setOpen] = useState(false);
  const [mentions, setMentions] = useState<MentionSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    if (!token) {
      setMentions([]);
      return;
    }
    setLoading(true);
    try {
      const list = await api.unreadMentions(token);
      setMentions(list);
      setError(null);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setLoading(false);
    }
  }, [token]);

  // Poll on mount and after role switch; keep the badge fresh while mounted.
  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 20000);
    return () => window.clearInterval(id);
  }, [refresh]);

  // Close the dropdown on an outside click.
  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  if (role === 'patient') return null;

  const toggle = () => {
    setOpen((prev) => {
      const next = !prev;
      if (next) void refresh();
      return next;
    });
  };

  const markRead = async (mentionId: string) => {
    try {
      await api.markMentionRead(token, mentionId);
      await refresh();
    } catch (err) {
      setError(messageOf(err));
    }
  };

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        onClick={toggle}
        aria-label="Notifications"
        className="relative flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 shadow-sm transition-colors hover:bg-slate-50 hover:text-slate-700"
      >
        <svg
          className="h-4 w-4"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={1.8}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0"
          />
        </svg>
        {mentions.length > 0 && (
          <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
            {mentions.length}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-40 mt-2 w-80 max-w-[80vw] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg">
          <div className="border-b border-slate-100 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
            Mentions
          </div>
          <div className="max-h-80 overflow-y-auto">
            {loading && <p className="px-3 py-3 text-xs text-slate-500">Loading…</p>}
            {!loading && error && <p className="px-3 py-3 text-xs text-red-600">{error}</p>}
            {!loading && !error && mentions.length === 0 && (
              <p className="px-3 py-3 text-sm text-slate-500">No unread mentions.</p>
            )}
            {!loading &&
              !error &&
              mentions.map((m) => (
                <div
                  key={m.id}
                  className="border-b border-slate-100 px-3 py-2 last:border-b-0"
                >
                  <div className="flex items-start justify-between gap-2">
                    <p className="line-clamp-2 min-w-0 text-xs text-slate-700">
                      {m.context?.comment_body ?? '(no comment snippet)'}
                    </p>
                    <button
                      type="button"
                      onClick={() => void markRead(m.id)}
                      className="shrink-0 rounded border border-slate-200 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 transition-colors hover:bg-slate-50"
                    >
                      Mark read
                    </button>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-slate-400">
                    <span className="capitalize">
                      {m.context?.comment_author_role ? roleLabel(m.context.comment_author_role) : 'teammate'}
                    </span>
                    <span>·</span>
                    <span>{m.context?.patient_name ?? 'patient'}</span>
                    <span>·</span>
                    <span className="font-mono">{formatDate(m.created_at)}</span>
                  </div>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
