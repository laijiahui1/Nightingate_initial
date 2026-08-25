// Tasks panel for clinical roles: task list (title, status, priority,
// assignee, due, description), an inline status select, and a create form
// (title, assignee from users(), priority, optional related entry).
//
// Rendered only for staff/clinician/admin — the patient role never mounts it.

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  api,
  ApiError,
  messageOf,
  type EntrySummary,
  type TaskPriority,
  type TaskStatus,
  type TaskSummary,
  type UserSummary,
} from './api';
import { formatDate } from './ui';

const STATUS_STYLES: Record<string, string> = {
  open: 'bg-slate-100 text-slate-700 ring-slate-200',
  in_progress: 'bg-sky-50 text-sky-700 ring-sky-200',
  done: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  cancelled: 'bg-slate-100 text-slate-500 ring-slate-200',
};

const PRIORITY_STYLES: Record<string, string> = {
  low: 'bg-slate-100 text-slate-600 ring-slate-200',
  medium: 'bg-amber-50 text-amber-700 ring-amber-200',
  high: 'bg-orange-50 text-orange-700 ring-orange-200',
  critical: 'bg-red-50 text-red-700 ring-red-200',
};

const STATUSES: { value: TaskStatus; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'done', label: 'Done' },
  { value: 'cancelled', label: 'Cancelled' },
];

const PRIORITIES: { value: TaskPriority; label: string }[] = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'critical', label: 'Critical' },
];

interface TasksPanelProps {
  token: string;
  patientId: string;
  entries: EntrySummary[];
  showToast: (message: string) => void;
}

export function TasksPanel({ token, patientId, entries, showToast }: TasksPanelProps) {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState('');
  const [assigneeId, setAssigneeId] = useState('');
  const [priority, setPriority] = useState<TaskPriority>('medium');
  const [entryId, setEntryId] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [taskList, userList] = await Promise.all([
        api.tasks(token, patientId),
        api.users(token),
      ]);
      setTasks(taskList);
      setUsers(userList);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setLoading(false);
    }
  }, [token, patientId]);

  useEffect(() => {
    void load();
  }, [load]);

  const userById = useMemo(() => {
    const map = new Map<string, UserSummary>();
    for (const u of users) map.set(u.id, u);
    return map;
  }, [users]);

  const openForm = () => {
    setShowForm(true);
    setEntryId(entries[0]?.id ?? '');
    setAssigneeId(users[0]?.id ?? '');
  };

  const createTask = async () => {
    if (!title.trim() || !assigneeId) return;
    setBusy(true);
    try {
      await api.createTask(token, patientId, {
        title: title.trim(),
        assignee_id: assigneeId,
        priority,
        entry_id: entryId || null,
      });
      setTitle('');
      setAssigneeId('');
      setPriority('medium');
      setEntryId('');
      setShowForm(false);
      await load();
      showToast('Task created.');
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) {
        showToast(err.message);
      } else {
        showToast(`Could not create task: ${messageOf(err)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const updateStatus = async (task: TaskSummary, status: TaskStatus) => {
    setBusy(true);
    try {
      await api.updateTask(token, task.id, { status });
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        showToast(err.message);
      } else {
        showToast(`Could not update task: ${messageOf(err)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold tracking-wide text-slate-900">Tasks</h3>
        <div className="flex items-center gap-3">
          {tasks.length > 0 && (
            <span className="text-xs text-slate-400">
              {tasks.length} {tasks.length === 1 ? 'task' : 'tasks'}
            </span>
          )}
          <button
            type="button"
            onClick={openForm}
            disabled={busy}
            className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-sky-700 disabled:opacity-50"
          >
            New task
          </button>
        </div>
      </div>

      {showForm && (
        <div className="mt-3 space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3">
          <label className="block">
            <span className="text-xs font-medium text-slate-600">Title</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Task title"
              className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 placeholder-slate-400 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="block">
              <span className="text-xs font-medium text-slate-600">Assignee</span>
              <select
                value={assigneeId}
                onChange={(e) => setAssigneeId(e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
              >
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.full_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs font-medium text-slate-600">Priority</span>
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value as TaskPriority)}
                className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
              >
                {PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs font-medium text-slate-600">Related entry</span>
              <select
                value={entryId}
                onChange={(e) => setEntryId(e.target.value)}
                className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200"
              >
                <option value="">None</option>
                {entries.map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.title}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void createTask()}
              disabled={busy || !title.trim() || !assigneeId}
              className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? 'Creating…' : 'Create task'}
            </button>
            <button
              type="button"
              onClick={() => {
                setShowForm(false);
                setTitle('');
              }}
              disabled={busy}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading && <p className="mt-3 text-xs text-slate-500">Loading tasks…</p>}

      {!loading && error && <p className="mt-3 text-xs text-red-600">{error}</p>}

      {!loading && !error && tasks.length === 0 && (
        <p className="mt-3 text-sm text-slate-500">No tasks for this patient yet.</p>
      )}

      {!loading && !error && tasks.length > 0 && (
        <ul className="mt-3 space-y-2">
          {tasks.map((task) => {
            const assignee = userById.get(task.assignee_id);
            return (
              <li
                key={task.id}
                className="rounded-lg border border-slate-100 bg-slate-50/60 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-slate-800">{task.title}</span>
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ring-1 ${STATUS_STYLES[task.status] ?? STATUS_STYLES.open}`}>
                    {task.status}
                  </span>
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ring-1 ${PRIORITY_STYLES[task.priority] ?? PRIORITY_STYLES.medium}`}>
                    {task.priority}
                  </span>
                  <select
                    value={task.status}
                    onChange={(e) => void updateStatus(task, e.target.value as TaskStatus)}
                    disabled={busy}
                    className="ml-auto rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-200 disabled:opacity-50"
                    aria-label={`Status for ${task.title}`}
                  >
                    {STATUSES.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
                  <span>
                    Assignee:{' '}
                    <span className="font-medium text-slate-700">
                      {assignee?.full_name ?? task.assignee_id.slice(0, 8)}
                    </span>
                  </span>
                  {task.due_at && <span>Due {formatDate(task.due_at)}</span>}
                  {task.completed_at && <span>Completed {formatDate(task.completed_at)}</span>}
                </div>
                {task.description && (
                  <p className="mt-1 text-xs text-slate-600">{task.description}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
