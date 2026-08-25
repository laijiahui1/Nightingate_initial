// Revision history for one entry: toggleable version list (vN, author role,
// time, body, conflict badge) with a "Restore vN" action per past version.
// Restore calls revertEntry and asks the parent to refresh the bundle.

import { useCallback, useState } from 'react';
import { api, messageOf, type VersionRow } from './api';
import { formatDate, roleLabel } from './ui';

interface HistoryPanelProps {
  token: string;
  entryId: string;
  currentVersion: number;
  onRestored: () => void;
  showToast: (message: string) => void;
}

export function HistoryPanel({ token, entryId, currentVersion, onRestored, showToast }: HistoryPanelProps) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<VersionRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await api.entryHistory(token, entryId);
      setVersions(list);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setLoading(false);
    }
  }, [token, entryId]);

  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (versions === null) void load();
  };

  const restore = async (version: number) => {
    setRestoring(version);
    try {
      await api.revertEntry(token, entryId, version);
      onRestored();
      showToast(`Restored entry to v${version}.`);
      await load();
    } catch (err) {
      showToast(`Could not restore: ${messageOf(err)}`);
    } finally {
      setRestoring(null);
    }
  };

  return (
    <div>
      <button
        type="button"
        onClick={toggle}
        className="text-xs font-medium text-slate-600 transition-colors hover:text-slate-800"
      >
        {open ? 'Hide history' : 'History'}
      </button>

      {open && (
        <div className="mt-2 space-y-2">
          {loading && <p className="text-xs text-slate-500">Loading history…</p>}
          {!loading && error && <p className="text-xs text-red-600">{error}</p>}
          {!loading && !error && versions !== null && versions.length === 0 && (
            <p className="text-xs text-slate-500">No revision history for this entry.</p>
          )}
          {!loading &&
            !error &&
            versions?.map((v) => {
              const current = v.version === currentVersion;
              return (
                <div
                  key={v.id}
                  className={`rounded-lg border px-3 py-2 ${
                    current ? 'border-sky-200 bg-sky-50/60' : 'border-slate-200 bg-white'
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2 text-[11px]">
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono font-semibold text-white">
                      v{v.version}
                    </span>
                    {current && (
                      <span className="rounded bg-sky-50 px-1.5 py-0.5 font-semibold text-sky-700 ring-1 ring-sky-200">
                        current
                      </span>
                    )}
                    {v.conflict_flag && (
                      <span
                        className="rounded bg-amber-50 px-1.5 py-0.5 font-semibold text-amber-700 ring-1 ring-amber-200"
                        title={v.conflict_of != null ? `Conflicts with v${v.conflict_of}` : 'Conflicting edit'}
                      >
                        conflict
                      </span>
                    )}
                    <span className="text-slate-600">{roleLabel(v.author_role)}</span>
                    <time className="font-mono text-slate-400">{formatDate(v.created_at)}</time>
                  </div>
                  <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs text-slate-600">{v.body}</p>
                  {!current && (
                    <button
                      type="button"
                      disabled={restoring === v.version}
                      onClick={() => void restore(v.version)}
                      className="mt-1 text-[11px] font-medium text-amber-600 transition-colors hover:text-amber-700 disabled:opacity-50"
                    >
                      {restoring === v.version ? 'Restoring…' : `Restore v${v.version}`}
                    </button>
                  )}
                </div>
              );
            })}
        </div>
      )}
    </div>
  );
}
