// Care Note main panel: Glance Top Card, longitudinal timeline, threaded
// comments (post/reply/resolve), clinical Tasks panel, per-entry revision
// history, AI Scribe box and inline edit on clinical entries.

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  api,
  ApiError,
  messageOf,
  type CommentSummary,
  type EntrySummary,
  type GlanceCard,
  type HighlightSummary,
  type PageBundle,
  type PatientSummary,
  type Role,
  type ScribeType,
} from './api';
import { Comments } from './Comments';
import { HighlightedBody } from './HighlightedBody';
import { HistoryPanel } from './HistoryPanel';
import { TasksPanel } from './TasksPanel';
import { RISK_STYLES, formatDate, roleBadge, roleLabel } from './ui';

const SCRIBE_TYPES: { value: ScribeType; label: string }[] = [
  { value: 'ai_doctor_consult_summary', label: 'Doctor consult summary' },
  { value: 'ai_nurse_consult_summary', label: 'Nurse consult summary' },
  { value: 'ai_patient_session_summary', label: 'Patient session summary' },
];

function sectionLabel(section: string | null): string {
  if (!section) return '';
  return section
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function riskLevelOf(entry: EntrySummary): string {
  return entry.risk_level || 'unknown';
}

function humanizeType(entryType: string): string {
  return entryType
    .replace(/^ai_/, 'AI ')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

interface CareNoteViewProps {
  token: string;
  role: Role;
  patient: PatientSummary | null;
  patientId: string;
}

export function CareNoteView({ token, role, patient, patientId }: CareNoteViewProps) {
  const [bundle, setBundle] = useState<PageBundle | null>(null);
  const [glance, setGlance] = useState<GlanceCard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editingEntryId, setEditingEntryId] = useState<string | null>(null);
  const [editBody, setEditBody] = useState('');
  const [saving, setSaving] = useState(false);

  const [scribeType, setScribeType] = useState<ScribeType>('ai_doctor_consult_summary');
  const [transcript, setTranscript] = useState('');
  const [scribing, setScribing] = useState(false);

  const [toast, setToast] = useState<string | null>(null);

  const [highlights, setHighlights] = useState<HighlightSummary[]>([]);
  const [generating, setGenerating] = useState(false);
  const [busyHighlightId, setBusyHighlightId] = useState<string | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(null), 6000);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.bundle(token, patientId);
      setBundle(page);
      if (role !== 'patient') {
        try {
          const g = await api.glance(token, patientId);
          setGlance(g);
        } catch {
          setGlance(null);
        }
        try {
          const result = await api.highlights(token, patientId);
          setHighlights(result.highlights);
        } catch {
          setHighlights([]);
        }
      } else {
        setHighlights([]);
      }
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setLoading(false);
    }
  }, [token, patientId, role]);

  // Reload only the highlight list after an accept/reject/generate so the
  // bundle stays put and the marks update in place.
  const refreshHighlights = useCallback(async () => {
    if (role === 'patient') return;
    try {
      const result = await api.highlights(token, patientId);
      setHighlights(result.highlights);
    } catch {
      // Keep the last known highlights; the next full load will retry.
    }
  }, [token, patientId, role]);

  useEffect(() => {
    void load();
  }, [load]);

  const entries = useMemo(() => {
    const list = bundle?.entries ?? [];
    return [...list].sort(
      (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
  }, [bundle]);

  const commentsByEntry = useMemo(() => {
    const map = new Map<string, CommentSummary[]>();
    for (const c of bundle?.comments ?? []) {
      const list = map.get(c.entry_id) ?? [];
      list.push(c);
      map.set(c.entry_id, list);
    }
    return map;
  }, [bundle]);

  const highlightsByEntry = useMemo(() => {
    const map = new Map<string, HighlightSummary[]>();
    for (const h of highlights) {
      const list = map.get(h.entry_id) ?? [];
      list.push(h);
      map.set(h.entry_id, list);
    }
    return map;
  }, [highlights]);

  const canEdit = (entry: EntrySummary): boolean =>
    role !== 'patient' && (entry.author_role === 'staff' || entry.author_role === 'clinician');

  const startEdit = (entry: EntrySummary) => {
    setEditingEntryId(entry.id);
    setEditBody(entry.body);
  };

  const cancelEdit = () => {
    setEditingEntryId(null);
    setEditBody('');
  };

  const saveEdit = async (entry: EntrySummary) => {
    setSaving(true);
    try {
      await api.editEntry(token, entry.id, editBody, entry.version);
      setEditingEntryId(null);
      setEditBody('');
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        showToast(err.message);
      } else {
        showToast(`Could not save edit: ${messageOf(err)}`);
      }
    } finally {
      setSaving(false);
    }
  };

  const generateSummary = async () => {
    if (!transcript.trim()) {
      showToast('Please paste a transcript to generate a summary.');
      return;
    }
    setScribing(true);
    try {
      await api.aiScribe(token, patientId, { scribe_type: scribeType, transcript });
      setTranscript('');
      await load();
      showToast('AI summary generated and redaction confirmed.');
    } catch (err) {
      showToast(`AI Scribe failed: ${messageOf(err)}`);
    } finally {
      setScribing(false);
    }
  };

  const resolveHighlight = async (highlight: HighlightSummary, action: 'accept' | 'reject') => {
    setBusyHighlightId(highlight.id);
    try {
      if (action === 'accept') {
        await api.acceptHighlight(token, patientId, highlight.id);
      } else {
        await api.rejectHighlight(token, patientId, highlight.id);
      }
      await refreshHighlights();
    } catch (err) {
      showToast(`Could not ${action} highlight: ${messageOf(err)}`);
    } finally {
      setBusyHighlightId(null);
    }
  };

  const generateHighlights = async () => {
    const aiEntry = [...entries]
      .filter((e) => e.author_role === 'system')
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())[0];
    if (!aiEntry) {
      showToast('No AI-scribed entry is available to highlight.');
      return;
    }
    setGenerating(true);
    try {
      await api.generateHighlight(token, patientId, aiEntry.id);
      await refreshHighlights();
      showToast('Risk highlights generated and marked as suggested.');
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        showToast(err.message);
      } else {
        showToast(`Could not generate highlights: ${messageOf(err)}`);
      }
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="space-y-6">
      {toast && (
        <div className="fixed bottom-4 left-1/2 z-50 w-full max-w-xl -translate-x-1/2 px-4">
          <div className="rounded-lg border border-slate-200 bg-slate-900 px-4 py-3 text-sm text-white shadow-lg">
            {toast}
          </div>
        </div>
      )}

      {/* Patient header */}
      <header className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-xl font-semibold tracking-tight text-slate-900">
            {patient?.display_name ?? 'Patient'}
          </h2>
          {patient?.mrn && <span className="font-mono text-xs text-slate-500">{patient.mrn}</span>}
          {patient?.gender && (
            <span className="text-sm capitalize text-slate-500">{patient.gender}</span>
          )}
        </div>
        <p className="mt-1 text-sm text-slate-500">Care note</p>
      </header>

      {loading && (
        <div className="rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500 shadow-sm">
          Loading care note…
        </div>
      )}

      {!loading && error && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm text-red-700 shadow-sm">
          {error}
        </div>
      )}

      {!loading && !error && (
        <>
          {/* Glance Top Card */}
          {role === 'patient' ? (
            <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <svg
                  className="h-4 w-4 text-slate-400"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.8}
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"
                  />
                </svg>
                <span className="font-medium text-slate-700">Access restricted</span>
                <span className="text-slate-400">— the clinical top card is not shown to the patient role.</span>
              </div>
            </section>
          ) : (
            <GlanceCardView glance={glance} />
          )}

          {/* AI Scribe box — clinical roles only */}
          {role !== 'patient' && (
            <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <h3 className="text-sm font-semibold tracking-wide text-slate-900">AI Scribe</h3>
              <p className="mt-1 text-xs text-slate-500">
                Paste a consult transcript; PHI is redacted before the summary is generated.
              </p>
              <div className="mt-3 space-y-3">
                <label className="block">
                  <span className="text-xs font-medium text-slate-600">Summary type</span>
                  <select
                    value={scribeType}
                    onChange={(e) => setScribeType(e.target.value as ScribeType)}
                    className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200 sm:w-72"
                  >
                    {SCRIBE_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                </label>
                <textarea
                  value={transcript}
                  onChange={(e) => setTranscript(e.target.value)}
                  rows={4}
                  placeholder="Paste the raw consultation transcript here…"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
                />
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => void generateSummary()}
                    disabled={scribing}
                    className="rounded-md bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition-colors hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {scribing ? 'Generating…' : 'Generate AI Summary'}
                  </button>
                  <span className="text-xs text-slate-400">
                    {transcript.length} chars
                  </span>
                </div>
              </div>
              <div className="mt-4 border-t border-slate-100 pt-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h4 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      Risk highlights
                    </h4>
                    <p className="mt-0.5 text-[11px] text-slate-500">
                      Generate suggestions from the most recent AI-scribed entry.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => void generateHighlights()}
                    disabled={generating}
                    className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {generating ? 'Generating…' : 'Generate highlights'}
                  </button>
                </div>
              </div>
            </section>
          )}

          {/* Tasks — clinical roles only */}
          {role !== 'patient' && (
            <TasksPanel token={token} patientId={patientId} entries={entries} showToast={showToast} />
          )}

          {/* Timeline */}
          <section>
            <h3 className="mb-3 text-sm font-semibold tracking-wide text-slate-900">
              Timeline · {entries.length} {entries.length === 1 ? 'entry' : 'entries'}
            </h3>
            {entries.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500 shadow-sm">
                No entries visible for this role yet.
              </div>
            ) : (
              <div className="space-y-4">
                {entries.map((entry) => (
                  <EntryCard
                    key={entry.id}
                    entry={entry}
                    canEdit={canEdit(entry)}
                    editing={editingEntryId === entry.id}
                    editBody={editBody}
                    saving={saving}
                    highlights={highlightsByEntry.get(entry.id) ?? []}
                    isClinical={role !== 'patient'}
                    busyHighlightId={busyHighlightId}
                    onAcceptHighlight={(h) => void resolveHighlight(h, 'accept')}
                    onRejectHighlight={(h) => void resolveHighlight(h, 'reject')}
                    onEditBodyChange={setEditBody}
                    onStartEdit={() => startEdit(entry)}
                    onCancelEdit={cancelEdit}
                    onSaveEdit={() => void saveEdit(entry)}
                    historySection={
                      role !== 'patient' ? (
                        <HistoryPanel
                          token={token}
                          entryId={entry.id}
                          currentVersion={entry.version}
                          onRestored={() => void load()}
                          showToast={showToast}
                        />
                      ) : undefined
                    }
                    commentsSection={
                      <Comments
                        token={token}
                        role={role}
                        entryId={entry.id}
                        comments={commentsByEntry.get(entry.id) ?? []}
                        onChanged={() => void load()}
                        showToast={showToast}
                      />
                    }
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Glance Top Card                                                     */
/* ------------------------------------------------------------------ */

function GlanceCardView({ glance }: { glance: GlanceCard | null }) {
  if (!glance) {
    return (
      <section className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm">
        Glance top card unavailable for this patient.
      </section>
    );
  }
  const hasContent =
    glance.top_items.length > 0 || glance.open_actions.length > 0 || glance.risk_flags.length > 0;

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold tracking-wide text-slate-900">Glance Top Card</h3>
        <span className="text-xs text-slate-400">computed {formatDate(glance.computed_at)}</span>
      </div>

      {!hasContent && (
        <p className="mt-3 text-sm text-slate-500">
          No top-card signals yet — computed card is empty.
        </p>
      )}

      {glance.top_items.length > 0 && (
        <div className="mt-4">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Top items</h4>
          <ul className="mt-2 space-y-2">
            {glance.top_items.map((item) => (
              <li
                key={item.item_id}
                className="flex items-start gap-3 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-slate-800">
                      {humanizeType(item.item_type)}
                    </span>
                    {item.risk_level !== 'low' && item.risk_level && (
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ring-1 ${RISK_STYLES[item.risk_level] ?? RISK_STYLES.low}`}>
                        {item.risk_level}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-xs text-slate-600">{item.preview}</p>
                </div>
                <span className="shrink-0 rounded bg-white px-1.5 py-0.5 font-mono text-xs font-semibold text-slate-600 ring-1 ring-slate-200">
                  {item.score}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {glance.open_actions.length > 0 && (
        <div className="mt-4">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Open actions</h4>
          <ul className="mt-2 space-y-1.5">
            {glance.open_actions.map((action) => (
              <li key={`${action.kind}-${action.id}`} className="flex items-center gap-2 text-sm">
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-600 ring-1 ring-slate-200">
                  {action.kind}
                </span>
                <span className="truncate text-slate-700">{action.label}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {glance.risk_flags.length > 0 && (
        <div className="mt-4">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Risk flags</h4>
          <ul className="mt-2 space-y-1.5">
            {glance.risk_flags.map((flag) => (
              <li key={flag.entry_id} className="flex items-center gap-2 text-sm">
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ring-1 ${RISK_STYLES[flag.risk_level] ?? RISK_STYLES.critical}`}>
                  {flag.risk_level}
                </span>
                <span className="truncate text-slate-700">{flag.title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Timeline entry card                                                 */
/* ------------------------------------------------------------------ */

interface EntryCardProps {
  entry: EntrySummary;
  canEdit: boolean;
  editing: boolean;
  editBody: string;
  saving: boolean;
  highlights: HighlightSummary[];
  isClinical: boolean;
  busyHighlightId: string | null;
  historySection?: ReactNode;
  commentsSection: ReactNode;
  onAcceptHighlight: (highlight: HighlightSummary) => void;
  onRejectHighlight: (highlight: HighlightSummary) => void;
  onEditBodyChange: (value: string) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
}

function EntryCard({
  entry,
  canEdit,
  editing,
  editBody,
  saving,
  highlights,
  isClinical,
  busyHighlightId,
  historySection,
  commentsSection,
  onAcceptHighlight,
  onRejectHighlight,
  onEditBodyChange,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
}: EntryCardProps) {
  return (
    <article className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3">
        <span
          className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ring-1 ${roleBadge(entry.author_role)}`}
        >
          {roleLabel(entry.author_role)}
        </span>
        <h4 className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-800">{entry.title}</h4>
        <time className="shrink-0 font-mono text-xs text-slate-400">
          {formatDate(entry.created_at)}
        </time>
      </div>

      <div className="px-4 py-3">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
          {entry.section && (
            <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-600">
              {sectionLabel(entry.section)}
            </span>
          )}
          <span
            className={`rounded-full px-2 py-0.5 font-semibold uppercase ring-1 ${RISK_STYLES[riskLevelOf(entry)] ?? RISK_STYLES.low}`}
          >
            {entry.risk_level || 'unknown'} risk
          </span>
          {entry.ai && (
            <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-600">
              {entry.ai.model_name ?? 'AI model'}
            </span>
          )}
          {entry.ai?.redaction_confirmed && (
            <span className="rounded-full bg-emerald-50 px-2 py-0.5 font-semibold text-emerald-700 ring-1 ring-emerald-200">
              ✓ Redaction confirmed
            </span>
          )}
          <span className="text-slate-400">v{entry.version}</span>
        </div>

        {editing ? (
          <div className="space-y-2">
            <textarea
              value={editBody}
              onChange={(e) => onEditBodyChange(e.target.value)}
              rows={6}
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
            />
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onSaveEdit}
                disabled={saving}
                className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                onClick={onCancelEdit}
                disabled={saving}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:opacity-50"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <HighlightedBody
            body={entry.body}
            highlights={highlights}
            isClinical={isClinical}
            busyHighlightId={busyHighlightId}
            onAccept={onAcceptHighlight}
            onReject={onRejectHighlight}
          />
        )}

        {!editing && canEdit && (
          <button
            type="button"
            onClick={onStartEdit}
            className="mt-2 text-xs font-medium text-sky-600 hover:text-sky-700"
          >
            Edit
          </button>
        )}
      </div>

      {historySection && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-4 py-3">
          {historySection}
        </div>
      )}

      <div className="border-t border-slate-100 bg-slate-50/60 px-4 py-3">
        {commentsSection}
      </div>
    </article>
  );
}
