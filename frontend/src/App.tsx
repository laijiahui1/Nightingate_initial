// Nightingale Care Note — demo SPA.
//
// Top bar role switcher (Patient / Staff / Clinician / Admin) → dev login
// (POST /api/auth/login) → patient list (GET /api/patients) → Care Note view.

import { useCallback, useEffect, useState } from 'react';
import {
  api,
  DEMO_EMAILS,
  messageOf,
  type LoginResult,
  type PatientSummary,
  type Role,
} from './api';
import { CareNoteView } from './CareNoteView';

const ROLE_TABS: { role: Role; label: string }[] = [
  { role: 'patient', label: 'Patient' },
  { role: 'staff', label: 'Staff' },
  { role: 'clinician', label: 'Clinician' },
  { role: 'admin', label: 'Admin' },
];

const ROLE_HINT: Record<Role, string> = {
  patient: 'Sees only their own record (patient_visible entries).',
  staff: 'Clinic staff — full clinic record view.',
  clinician: 'Clinician — full clinic record view.',
  admin: 'Administrator — full clinic record view.',
};

export default function App() {
  const [login, setLogin] = useState<LoginResult | null>(null);
  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [selectedPatientId, setSelectedPatientId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyRole, setBusyRole] = useState<Role | null>(null);

  const switchRole = useCallback(async (role: Role) => {
    setBusyRole(role);
    setError(null);
    setSelectedPatientId(null);
    try {
      const result = await api.login(DEMO_EMAILS[role]);
      setLogin(result);
      const list = await api.patients(result.access_token);
      setPatients(list);
      setSelectedPatientId(list[0]?.id ?? null);
    } catch (err) {
      setError(messageOf(err));
      setLogin(null);
      setPatients([]);
    } finally {
      setBusyRole(null);
    }
  }, []);

  // Open the demo on the Clinician role so there is immediate content.
  useEffect(() => {
    void switchRole('clinician');
  }, [switchRole]);

  const selectedPatient = patients.find((p) => p.id === selectedPatientId) ?? null;

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      <div className="mx-auto flex max-w-6xl flex-col px-4 sm:px-6">
        {/* Top bar */}
        <header className="border-b border-slate-200 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="text-lg font-semibold tracking-tight text-slate-900">Nightingale</h1>
              <p className="text-xs text-slate-500">Care note collaboration workspace</p>
            </div>

            <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1 shadow-sm">
              {ROLE_TABS.map((tab) => {
                const active = login?.role === tab.role;
                return (
                  <button
                    key={tab.role}
                    type="button"
                    onClick={() => void switchRole(tab.role)}
                    disabled={busyRole !== null}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-wait ${
                      active
                        ? 'bg-slate-800 text-white shadow-sm'
                        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                    }`}
                  >
                    {busyRole === tab.role ? '…' : tab.label}
                  </button>
                );
              })}
            </div>
          </div>

          {login && (
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
              <span className="font-medium text-slate-700">{login.full_name}</span>
              <span className="font-mono text-slate-400">{login.email}</span>
              <span className="capitalize">
                {login.role} role · {ROLE_HINT[login.role]}
              </span>
            </div>
          )}
        </header>

        {error && (
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 shadow-sm">
            {error}
          </div>
        )}

        {/* Body */}
        <main className="flex-1 gap-6 py-6 lg:grid lg:grid-cols-[280px_1fr]">
          {/* Patient list */}
          <aside className="mb-6 lg:mb-0">
            <div className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
              <h2 className="px-1 pb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
                Patients
              </h2>
              {patients.length === 0 && !error && (
                <p className="px-1 py-3 text-sm text-slate-500">No patients for this role.</p>
              )}
              <ul className="space-y-1">
                {patients.map((p) => {
                  const active = p.id === selectedPatientId;
                  return (
                    <li key={p.id}>
                      <button
                        type="button"
                        onClick={() => setSelectedPatientId(p.id)}
                        className={`w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                          active
                            ? 'border-sky-200 bg-sky-50'
                            : 'border-transparent hover:bg-slate-50'
                        }`}
                      >
                        <span
                          className={`block text-sm font-medium ${
                            active ? 'text-sky-900' : 'text-slate-800'
                          }`}
                        >
                          {p.display_name}
                        </span>
                        <span className="mt-0.5 flex items-center gap-2 font-mono text-[11px] text-slate-500">
                          <span>{p.mrn}</span>
                          {p.gender && (
                            <span className="normal-case">{p.gender}</span>
                          )}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          </aside>

          {/* Care note panel */}
          <section className="min-w-0">
            {login && selectedPatientId ? (
              <CareNoteView
                key={`${login.role}:${selectedPatientId}`}
                token={login.access_token}
                role={login.role}
                patient={selectedPatient}
                patientId={selectedPatientId}
              />
            ) : (
              <div className="rounded-xl border border-slate-200 bg-white p-8 text-center text-sm text-slate-500 shadow-sm">
                Select a role and a patient to open the care note.
              </div>
            )}
          </section>
        </main>
      </div>
    </div>
  );
}
