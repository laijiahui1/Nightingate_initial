// Span-stable risk highlighting for entry bodies.
//
// Highlights render by slicing the authored body at their stored offsets and
// wrapping the quoted span in a risk-colored <mark>. If the body was edited
// and the offsets no longer slice to the quoted text, the frozen
// quoted_text is re-located with indexOf so marks survive edits. Clicking a
// mark opens a small card with the risk reason, level, source, status and —
// for suggested highlights — Accept / Reject controls.

import { useMemo, useState } from 'react';
import type { HighlightSummary } from './api';
import { RISK_STYLES, formatDate } from './ui';

interface HighlightedBodyProps {
  body: string;
  highlights: HighlightSummary[];
  isClinical: boolean;
  busyHighlightId: string | null;
  onAccept: (highlight: HighlightSummary) => void;
  onReject: (highlight: HighlightSummary) => void;
}

interface LocatedSpan {
  start: number;
  end: number;
  highlight: HighlightSummary;
}

interface Segment {
  start: number;
  end: number;
  highlight: HighlightSummary | null;
}

/** Resolve every highlight to an exact span in the current body. */
function locateSpans(body: string, highlights: HighlightSummary[]): LocatedSpan[] {
  const spans: LocatedSpan[] = [];
  for (const h of highlights) {
    if (!h.quoted_text) continue;
    const sliceMatches =
      h.offset_start >= 0 &&
      h.offset_end <= body.length &&
      h.offset_end > h.offset_start &&
      body.slice(h.offset_start, h.offset_end) === h.quoted_text;
    const start = sliceMatches ? h.offset_start : body.indexOf(h.quoted_text);
    const end = start >= 0 ? start + h.quoted_text.length : -1;
    if (start >= 0 && end <= body.length) spans.push({ start, end, highlight: h });
  }
  spans.sort((a, b) => a.start - b.start || b.end - a.end);
  return spans;
}

/** Split the body into plain text and marked spans, in order. */
function buildSegments(body: string, spans: LocatedSpan[]): Segment[] {
  const segments: Segment[] = [];
  let cursor = 0;
  for (const span of spans) {
    if (span.start < cursor) continue; // overlapping span — keep the earliest
    if (span.start > cursor) segments.push({ start: cursor, end: span.start, highlight: null });
    segments.push({ start: span.start, end: span.end, highlight: span.highlight });
    cursor = span.end;
  }
  if (cursor < body.length) segments.push({ start: cursor, end: body.length, highlight: null });
  return segments;
}

function markStyle(level: string): string {
  return RISK_STYLES[level] ?? RISK_STYLES.low;
}

function formatConfidence(value: number | null): string {
  if (value === null) return 'n/a';
  return `${Math.round(value * 100)}%`;
}

export function HighlightedBody({
  body,
  highlights,
  isClinical,
  busyHighlightId,
  onAccept,
  onReject,
}: HighlightedBodyProps) {
  const [openId, setOpenId] = useState<string | null>(null);

  const spans = useMemo(() => locateSpans(body, highlights), [body, highlights]);
  const segments = useMemo(() => buildSegments(body, spans), [body, spans]);
  const open = spans.find((s) => s.highlight.id === openId)?.highlight ?? null;

  return (
    <div>
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">
        {segments.map((seg, i) => {
          if (!seg.highlight) {
            return <span key={i}>{body.slice(seg.start, seg.end)}</span>;
          }
          const h = seg.highlight;
          const active = openId === h.id;
          const stateClass =
            h.status === 'rejected'
              ? ' opacity-60 line-through'
              : h.status === 'suggested'
                ? ' ring-1 ring-slate-300'
                : '';
          return (
            <mark
              key={h.id}
              onClick={() => setOpenId(active ? null : h.id)}
              title={`${h.risk_level} · ${h.risk_reason}`}
              className={`${markStyle(h.risk_level)} cursor-pointer rounded px-0.5 py-0.5 transition-shadow${
                stateClass
              }${active ? ' ring-2 ring-slate-500' : ''}`}
            >
              {h.quoted_text}
              {h.status === 'accepted' && (
                <span className="ml-0.5 font-semibold" aria-label="accepted">
                  ✓
                </span>
              )}
            </mark>
          );
        })}
      </p>

      {open && (
        <div className="mt-2 space-y-2 rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ring-1 ${markStyle(
                open.risk_level,
              )}`}
            >
              {open.risk_level} risk
            </span>
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
              {open.source}
            </span>
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">
              {open.status}
            </span>
            <span className="ml-auto font-mono text-[10px] text-slate-400">
              confidence {formatConfidence(open.confidence)}
            </span>
          </div>
          <p className="text-sm text-slate-700">{open.risk_reason}</p>
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
            {isClinical && open.status === 'suggested' && (
              <>
                <button
                  type="button"
                  disabled={busyHighlightId === open.id}
                  onClick={() => onAccept(open)}
                  className="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busyHighlightId === open.id ? '…' : 'Accept'}
                </button>
                <button
                  type="button"
                  disabled={busyHighlightId === open.id}
                  onClick={() => onReject(open)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busyHighlightId === open.id ? '…' : 'Reject'}
                </button>
              </>
            )}
            {open.status !== 'suggested' && (
              <span className="font-medium text-slate-500">
                {open.status === 'accepted' ? '✓ Accepted' : 'Rejected'}
              </span>
            )}
            <span className="ml-auto text-slate-400">added {formatDate(open.created_at)}</span>
          </div>
        </div>
      )}
    </div>
  );
}
