// Learned-importance chips for the open AI-scribed entry.
//
// M6 closes the learning loop: when a clinician accepts/rejects a suggested
// highlight, the backend bumps learning_weight for that entry's entities. This
// panel queries GET /highlights/suggestions and shows the open entry's learned
// entities as chips (e.g. "amlodipine · +1.0"). Patient role never sees it.

import { useCallback, useEffect, useState } from 'react';
import { api, messageOf, type Suggestion } from './api';

interface ImportancePanelProps {
  token: string;
  patientId: string;
  entryId: string | null;
  onToast: (message: string) => void;
}

function scoreLabel(score: number): string {
  return score >= 0 ? `+${score.toFixed(1)}` : score.toFixed(1);
}

export function ImportancePanel({ token, patientId, entryId, onToast }: ImportancePanelProps) {
  const [chips, setChips] = useState<Suggestion[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!entryId) {
      setChips([]);
      return;
    }
    setLoading(true);
    try {
      const result = await api.suggestions(token, patientId, '');
      // Keep the entities belonging to the open entry that carry a learned
      // signal (weight != 0); untouched entities are not "learned" yet.
      setChips(result.suggestions.filter((s) => s.entry_id === entryId && s.score !== 0));
    } catch (err) {
      setChips([]);
      onToast(`Could not load learned importance: ${messageOf(err)}`);
    } finally {
      setLoading(false);
    }
  }, [token, patientId, entryId, onToast]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold tracking-wide text-slate-900">Learned importance</h3>
        {loading && <span className="text-xs text-slate-400">Loading…</span>}
      </div>
      <p className="mt-1 text-xs text-slate-500">
        Entities ranked by accepted and rejected highlight feedback on this entry.
      </p>
      {chips.length === 0 ? (
        <p className="mt-3 text-sm text-slate-500">No learned importance signals yet.</p>
      ) : (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {chips.map((s) => (
            <span
              key={s.feature_key}
              className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700 ring-1 ring-slate-200"
            >
              {s.entity_value} · <span className="font-semibold">{scoreLabel(s.score)}</span>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
