// Threaded comments panel: top-level posting, replies, resolve/reopen, and an
// @mention helper hint. Resolve is clinical-only; the backend enforces the
// author-role rule and returns 403, which we surface as a toast.

import { useMemo, useState, type ReactNode } from 'react';
import {
  api,
  ApiError,
  messageOf,
  type CommentSummary,
  type Role,
} from './api';
import { formatDate, roleBadge, roleLabel } from './ui';

interface CommentsProps {
  token: string;
  role: Role;
  entryId: string;
  comments: CommentSummary[];
  onChanged: () => void;
  showToast: (message: string) => void;
}

export function Comments({ token, role, entryId, comments, onChanged, showToast }: CommentsProps) {
  const [composing, setComposing] = useState(false);
  const [newBody, setNewBody] = useState('');
  const [posting, setPosting] = useState(false);
  const [replyToId, setReplyToId] = useState<string | null>(null);
  const [replyBody, setReplyBody] = useState('');
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const isClinical = role !== 'patient';

  const post = async () => {
    if (!newBody.trim()) return;
    setPosting(true);
    try {
      await api.createComment(token, entryId, { body: newBody.trim() });
      setNewBody('');
      setComposing(false);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        showToast(err.message);
      } else {
        showToast(`Could not post comment: ${messageOf(err)}`);
      }
    } finally {
      setPosting(false);
    }
  };

  const postReply = async () => {
    if (!replyToId || !replyBody.trim()) return;
    setPosting(true);
    try {
      await api.createComment(token, entryId, {
        body: replyBody.trim(),
        parent_id: replyToId,
      });
      setReplyBody('');
      setReplyToId(null);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        showToast(err.message);
      } else {
        showToast(`Could not post reply: ${messageOf(err)}`);
      }
    } finally {
      setPosting(false);
    }
  };

  const toggleResolve = async (comment: CommentSummary) => {
    setResolvingId(comment.id);
    try {
      if (comment.status === 'resolved') {
        await api.unresolveComment(token, comment.id);
      } else {
        await api.resolveComment(token, comment.id);
      }
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        showToast(err.message);
      } else {
        showToast(`Could not update comment: ${messageOf(err)}`);
      }
    } finally {
      setResolvingId(null);
    }
  };

  const byParent = useMemo(() => {
    const map = new Map<string | null, CommentSummary[]>();
    for (const c of comments) {
      const key = c.parent_id;
      const list = map.get(key) ?? [];
      list.push(c);
      map.set(key, list);
    }
    return map;
  }, [comments]);

  const renderLevel = (parentId: string | null): ReactNode[] =>
    (byParent.get(parentId) ?? []).map((c) => (
      <div key={c.id} className={parentId !== null ? 'ml-5 border-l border-slate-200 pl-3' : ''}>
        <div className="flex items-center gap-2">
          <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1 ${roleBadge(c.author_role)}`}>
            {roleLabel(c.author_role)}
          </span>
          <span className="font-mono text-[11px] text-slate-400">{formatDate(c.created_at)}</span>
          {c.status === 'resolved' && (
            <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
              resolved
            </span>
          )}
        </div>
        <p className="mt-0.5 whitespace-pre-wrap text-sm text-slate-700">{c.body}</p>
        <div className="mt-1 flex items-center gap-3">
          <button
            type="button"
            onClick={() => {
              setReplyToId(replyToId === c.id ? null : c.id);
              setReplyBody('');
            }}
            className="text-[11px] font-medium text-sky-600 hover:text-sky-700"
          >
            Reply
          </button>
          {isClinical && (
            <button
              type="button"
              disabled={resolvingId === c.id}
              onClick={() => void toggleResolve(c)}
              className="text-[11px] font-medium text-slate-500 transition-colors hover:text-slate-700 disabled:opacity-50"
            >
              {resolvingId === c.id ? '…' : c.status === 'resolved' ? 'Reopen' : 'Resolve'}
            </button>
          )}
        </div>

        {replyToId === c.id && (
          <div className="mt-2 space-y-2">
            <textarea
              value={replyBody}
              onChange={(e) => setReplyBody(e.target.value)}
              rows={2}
              placeholder="Write a reply…"
              className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
            />
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void postReply()}
                disabled={posting || !replyBody.trim()}
                className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {posting ? 'Posting…' : 'Post reply'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setReplyToId(null);
                  setReplyBody('');
                }}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {renderLevel(c.id)}
      </div>
    ));

  return (
    <div className="space-y-3">
      <h4 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Comments</h4>

      {composing ? (
        <div className="space-y-2">
          <textarea
            value={newBody}
            onChange={(e) => setNewBody(e.target.value)}
            rows={3}
            placeholder="Add a comment…"
            className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
          />
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => void post()}
              disabled={posting || !newBody.trim()}
              className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {posting ? 'Posting…' : 'Post comment'}
            </button>
            <button
              type="button"
              onClick={() => {
                setComposing(false);
                setNewBody('');
              }}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50"
            >
              Cancel
            </button>
          </div>
          {isClinical && (
            <p className="text-[11px] text-slate-400">Use @name to notify a teammate.</p>
          )}
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setComposing(true)}
          className="text-xs font-medium text-sky-600 hover:text-sky-700"
        >
          Add comment
        </button>
      )}

      {comments.length > 0 && <div className="space-y-2">{renderLevel(null)}</div>}
    </div>
  );
}
