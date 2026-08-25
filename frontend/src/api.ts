// Nightingale Care Note — API client and shared types.
//
// Talks to the FastAPI backend at the CORS-enabled origin. Single API_BASE
// const as required by the M2 contract. M3 adds collaboration types and
// methods: comments (threaded + resolve), @mentions, tasks, users, and entry
// revision history.

export const API_BASE = 'http://localhost:8000';

export type Role = 'patient' | 'staff' | 'clinician' | 'admin';

export type EntryAuthorRole = 'patient' | 'staff' | 'clinician' | 'system';

export type ScribeType =
  | 'ai_doctor_consult_summary'
  | 'ai_nurse_consult_summary'
  | 'ai_patient_session_summary';

export type CommentVisibility = 'internal' | 'patient_visible';

export type TaskStatus = 'open' | 'in_progress' | 'done' | 'cancelled';

export type TaskPriority = 'low' | 'medium' | 'high' | 'critical';

// Demo login emails per role (from backend/app/seed.py, Meridian clinic).
export const DEMO_EMAILS: Record<Role, string> = {
  patient: 'alice.tan@meridian.demo',
  staff: 'priya.nair@meridian.demo',
  clinician: 'marcus.lee@meridian.demo',
  admin: 'sandra.ho@meridian.demo',
};

export interface LoginResult {
  access_token: string;
  token_type: 'bearer';
  role: Role;
  clinic_id: string;
  full_name: string;
  email: string;
}

export interface PatientSummary {
  id: string;
  mrn: string;
  display_name: string;
  date_of_birth: string | null;
  gender: string | null;
}

export interface EntryAiMeta {
  ai_note_type: string | null;
  model_name: string | null;
  redaction_confirmed: boolean | null;
}

export interface EntrySummary {
  id: string;
  entry_type: string;
  title: string;
  body: string;
  author_role: EntryAuthorRole;
  section: string | null;
  visibility: 'patient_visible' | 'internal';
  risk_level: 'low' | 'medium' | 'high' | 'critical';
  version: number;
  status: string;
  created_at: string;
  updated_at: string | null;
  ai: EntryAiMeta | null;
}

export interface CommentCreate {
  body: string;
  parent_id?: string | null;
  visibility?: CommentVisibility | null;
}

export interface CommentSummary {
  id: string;
  entry_id: string;
  parent_id: string | null;
  author_role: EntryAuthorRole;
  author_id: string | null;
  patient_id: string | null;
  body: string;
  status: string;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface TaskCreate {
  title: string;
  description?: string | null;
  entry_id?: string | null;
  assignee_id: string;
  priority?: TaskPriority;
  due_at?: string | null;
}

export interface TaskUpdate {
  title?: string | null;
  description?: string | null;
  status?: TaskStatus | null;
  assignee_id?: string | null;
  priority?: TaskPriority | null;
  due_at?: string | null;
}

export interface TaskSummary {
  id: string;
  patient_id: string;
  entry_id: string | null;
  assignee_id: string;
  assigner_id: string;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: TaskPriority;
  due_at: string | null;
  created_at: string;
  updated_at: string | null;
  completed_at: string | null;
}

export interface UserSummary {
  id: string;
  full_name: string;
  email: string;
  role: string;
}

export interface MentionContext {
  comment_body: string | null;
  comment_author_role: string | null;
  entry_title: string | null;
  entry_id: string | null;
  patient_name: string | null;
  patient_id: string | null;
}

export interface MentionSummary {
  id: string;
  comment_id: string | null;
  entry_id: string | null;
  mentioned_user_id: string;
  created_by: string | null;
  read_at: string | null;
  created_at: string;
  context: MentionContext | null;
}

export interface VersionRow {
  id: number;
  version: number;
  body: string;
  delta_from_prev: Record<string, unknown> | null;
  author_role: string | null;
  author_id: string | null;
  change_summary: string | null;
  conflict_flag: boolean;
  conflict_of: number | null;
  created_at: string;
}

export interface PageBundle {
  entries: EntrySummary[];
  comments: CommentSummary[];
  tasks: TaskSummary[];
}

export interface GlanceItem {
  item_type: string;
  item_id: string;
  preview: string;
  risk_level: string;
  score: number;
  created_at: string | null;
}

export interface OpenAction {
  kind: string;
  id: string;
  label: string;
}

export interface RiskFlag {
  entry_id: string;
  risk_level: string;
  title: string;
}

export interface GlanceCard {
  patient_id: string;
  top_items: GlanceItem[];
  open_actions: OpenAction[];
  risk_flags: RiskFlag[];
  computed_at: string | null;
  invalidated: boolean;
}

export interface AiScribeResult {
  entry_id: string;
  entry_type: string;
  ai_note_type: string;
  model_name: string;
  redaction_confirmed: boolean;
  redaction_mask: Record<string, number> | null;
  visibility: string;
}

// --- M4 risk highlights ---------------------------------------------------

export type HighlightRiskLevel = 'low' | 'medium' | 'high' | 'critical';

export type HighlightSource = 'ai' | 'rule' | 'manual';

export type HighlightStatus = 'suggested' | 'accepted' | 'rejected';

export interface HighlightGenerateRequest {
  entry_id: string;
}

export interface HighlightSummary {
  id: string;
  patient_id: string;
  entry_id: string;
  offset_start: number;
  offset_end: number;
  quoted_text: string;
  risk_reason: string;
  risk_level: HighlightRiskLevel;
  source: HighlightSource;
  status: HighlightStatus;
  confidence: number | null;
  provenance_id: string | null;
  created_by: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
}

export interface HighlightListResult {
  highlights: HighlightSummary[];
}

export interface EditedEntry {
  id: string;
  version: number;
  body: string;
  updated_at: string | null;
}

export interface RevertResult {
  id: string;
  version: number;
  body: string;
}

export interface ResolveResult {
  id: string;
  status: string;
  resolved_by: string | null;
  resolved_at: string | null;
}

export interface MarkReadResult {
  id: string;
  read_at: string;
}

/** Error carrying the HTTP status so the UI can special-case 403. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function messageOf(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return 'Unexpected error';
}

interface RequestOptions {
  method?: string;
  token?: string;
  body?: unknown;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', token, body } = options;
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, 'Cannot reach the Nightingale API server. Is the backend running?');
  }

  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === 'string') message = data.detail;
    } catch {
      // Non-JSON error body — keep the status text.
    }
    throw new ApiError(res.status, message);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  login: (email: string) =>
    request<LoginResult>('/api/auth/login', { method: 'POST', body: { email } }),

  patients: (token: string) => request<PatientSummary[]>('/api/patients', { token }),

  bundle: (token: string, patientId: string) =>
    request<PageBundle>(`/api/patients/${patientId}`, { token }),

  glance: (token: string, patientId: string) =>
    request<GlanceCard>(`/api/patients/${patientId}/glance`, { token }),

  aiScribe: (token: string, patientId: string, payload: { scribe_type: ScribeType; transcript: string }) =>
    request<AiScribeResult>(`/api/patients/${patientId}/ai-scribe`, {
      method: 'POST',
      token,
      body: payload,
    }),

  editEntry: (token: string, entryId: string, body: string, baseVersion: number) =>
    request<EditedEntry>(`/api/entries/${entryId}`, {
      method: 'PUT',
      token,
      body: { body, base_version: baseVersion },
    }),

  // --- M3 collaboration --------------------------------------------------

  createComment: (token: string, entryId: string, payload: CommentCreate) =>
    request<CommentSummary>(`/api/entries/${entryId}/comments`, {
      method: 'POST',
      token,
      body: payload,
    }),

  resolveComment: (token: string, commentId: string) =>
    request<ResolveResult>(`/api/comments/${commentId}/resolve`, { method: 'POST', token }),

  unresolveComment: (token: string, commentId: string) =>
    request<ResolveResult>(`/api/comments/${commentId}/unresolve`, { method: 'POST', token }),

  tasks: (token: string, patientId: string) =>
    request<TaskSummary[]>(`/api/patients/${patientId}/tasks`, { token }),

  createTask: (token: string, patientId: string, payload: TaskCreate) =>
    request<TaskSummary>(`/api/patients/${patientId}/tasks`, { method: 'POST', token, body: payload }),

  updateTask: (token: string, taskId: string, patch: TaskUpdate) =>
    request<TaskSummary>(`/api/tasks/${taskId}`, { method: 'PATCH', token, body: patch }),

  users: (token: string) => request<UserSummary[]>('/api/users', { token }),

  unreadMentions: (token: string) => request<MentionSummary[]>('/api/mentions/unread', { token }),

  markMentionRead: (token: string, mentionId: string) =>
    request<MarkReadResult>(`/api/mentions/${mentionId}/read`, { method: 'POST', token }),

  entryHistory: (token: string, entryId: string) =>
    request<VersionRow[]>(`/api/entries/${entryId}/history`, { token }),

  revertEntry: (token: string, entryId: string, targetVersion: number) =>
    request<RevertResult>(`/api/entries/${entryId}/revert`, {
      method: 'POST',
      token,
      body: { target_version: targetVersion },
    }),

  // --- M4 risk highlights ------------------------------------------------

  highlights: (token: string, patientId: string) =>
    request<HighlightListResult>(`/api/patients/${patientId}/highlights`, { token }),

  generateHighlight: (token: string, patientId: string, entryId: string) =>
    request<HighlightSummary>(`/api/patients/${patientId}/highlights/generate`, {
      method: 'POST',
      token,
      body: { entry_id: entryId } satisfies HighlightGenerateRequest,
    }),

  acceptHighlight: (token: string, patientId: string, highlightId: string) =>
    request<HighlightSummary>(`/api/patients/${patientId}/highlights/${highlightId}/accept`, {
      method: 'POST',
      token,
      body: {},
    }),

  rejectHighlight: (token: string, patientId: string, highlightId: string) =>
    request<HighlightSummary>(`/api/patients/${patientId}/highlights/${highlightId}/reject`, {
      method: 'POST',
      token,
      body: {},
    }),
};
