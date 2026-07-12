import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Check, ChevronLeft, ChevronRight, LoaderCircle, UserRound } from "lucide-react";
import { api, idOf, listOf, str } from "./api";
import { useResource } from "./hooks";

export function Page(props: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  return <main className="vela-main"><header className="vela-page-header"><div><h1>{props.title}</h1>{props.subtitle && <p>{props.subtitle}</p>}</div>{props.actions && <div className="vela-page-actions">{props.actions}</div>}</header>{props.children}</main>;
}

export function Panel(props: { title?: string; children: ReactNode; className?: string }) {
  return <section className={`vela-panel panel ${props.className ?? ""}`}>{props.title && <div className="vela-section-heading"><h2>{props.title}</h2></div>}{props.children}</section>;
}

export function Notice({ error, loading, empty }: { error?: string; loading?: boolean; empty?: boolean }) {
  if (error) return <div role="alert" className="vela-notice vela-notice-error"><strong>Unable to load</strong><span>{error}</span></div>;
  if (loading) return <div className="vela-skeleton" aria-label="Loading"><span /><span /><span /></div>;
  if (empty) return <div className="vela-empty"><span>No records yet</span><small>This area will update when matching work is available.</small></div>;
  return null;
}

export function ProfileSelect({ compact = false, workspaceName = "" }: { compact?: boolean; workspaceName?: string }) {
  const resource = useResource<unknown>("/api/profiles");
  const serverActive = useResource<Record<string, unknown>>("/api/profiles/active");
  const profiles = listOf<Record<string, unknown>>(resource.data).filter(profile => profile.is_active !== false);
  const [active, setActive] = useState(() => localStorage.getItem("titles.activeProfileId") ?? "");
  const [error, setError] = useState("");
  useEffect(() => { if (!active && serverActive.data?.id) setActive(idOf(serverActive.data)); }, [active, serverActive.data]);
  return <div className={`vela-profile-select ${compact ? "is-compact" : ""}`} title={error || `Active comment profile${workspaceName ? ` in ${workspaceName}` : ""}`}><UserRound size={16} aria-hidden="true" /><label className={compact ? "sr-only" : ""} htmlFor="active-profile">Active profile{workspaceName ? ` in ${workspaceName}` : ""}</label><select id="active-profile" aria-label={`Active profile${workspaceName ? ` in ${workspaceName}` : ""}`} value={active} onChange={async event => { const profileId = event.target.value; const previous = active; setActive(profileId); setError(""); try { if (profileId) { await api(`/api/profiles/${profileId}/active`, { method: "PUT" }); localStorage.setItem("titles.activeProfileId", profileId); } else { localStorage.removeItem("titles.activeProfileId"); } location.reload(); } catch (reason) { setActive(previous); setError(reason instanceof Error ? reason.message : String(reason)); } }}><option value="">{workspaceName ? `${workspaceName} profile` : "Profile"}</option>{profiles.map(profile => <option key={idOf(profile)} value={idOf(profile)}>{str(profile.display_name ?? profile.name)}</option>)}</select>{error && <span className="sr-only" role="alert">{error}</span>}</div>;
}

export function Field(props: { label: string; children: ReactNode; hint?: string }) {
  return <label className="vela-field field"><span>{props.label}</span>{props.children}{props.hint && <small>{props.hint}</small>}</label>;
}

export function Form(props: { onSubmit: (form: FormData) => void | Promise<void>; children: ReactNode; submit?: string }) {
  const [error, setError] = useState(""); const [saving, setSaving] = useState(false); const [saved, setSaved] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); setError(""); setSaved(false); setSaving(true); try { await props.onSubmit(new FormData(event.currentTarget)); setSaved(true); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setSaving(false); } };
  return <form className="vela-form" onSubmit={event => void submit(event)}>{props.children}{error && <div role="alert" className="vela-notice vela-notice-error">{error}</div>}<button className="vela-button vela-button-primary" type="submit" disabled={saving}>{saving ? <><LoaderCircle className="vela-spin" size={15} /> Working...</> : saved ? <><Check size={15} /> Saved</> : props.submit ?? "Save"}</button></form>;
}

export function Json({ value }: { value: unknown }) { return <pre className="vela-code">{JSON.stringify(value, null, 2)}</pre>; }

export function Pagination({ page, pageSize, total, onPage, onPageSize }: { page: number; pageSize: number; total: number; onPage: (page: number) => void; onPageSize: (size: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const start = total ? (page - 1) * pageSize + 1 : 0;
  const end = Math.min(page * pageSize, total);
  return <nav className="vela-pagination" aria-label="Pagination">
    <span>{start}–{end} of {total}</span>
    <label>Per page <select value={pageSize} onChange={event => onPageSize(Number(event.target.value))}>{[50, 100, 250].map(size => <option key={size} value={size}>{size}</option>)}</select></label>
    <button aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft size={15} /></button>
    <span>Page {page} of {pages}</span>
    <button aria-label="Next page" disabled={page >= pages} onClick={() => onPage(page + 1)}><ChevronRight size={15} /></button>
  </nav>;
}

export function DataTable(props: { rows: Record<string, unknown>[]; columns: Array<[string, string]>; onRow?: (row: Record<string, unknown>) => void }) {
  return <div className="vela-table-wrap table-wrap"><table className="vela-table"><thead><tr>{props.columns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>{props.rows.map((row, index) => <tr key={String(row.id ?? index)} role={props.onRow ? "link" : undefined} aria-label={props.onRow ? `Open ${str(row[props.columns[0]?.[0]], "record")}` : undefined} onClick={() => props.onRow?.(row)} onKeyDown={event => { if (props.onRow && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); props.onRow(row); } }} tabIndex={props.onRow ? 0 : undefined} className={props.onRow ? "is-clickable" : ""}>{props.columns.map(([key]) => <td key={key}>{renderCell(row[key])}</td>)}</tr>)}</tbody></table></div>;
}

function renderCell(value: unknown): ReactNode { if (typeof value === "boolean") return value ? "Yes" : "No"; if (Array.isArray(value)) return value.map(String).join(", "); if (value && typeof value === "object") return JSON.stringify(value); if (typeof value === "string" && /^https?:\/\//.test(value)) return <a href={value} target="_blank" rel="noreferrer" onClick={event => event.stopPropagation()}>Open</a>; return str(value); }

export function SelectRecords(props: { name: string; records: Record<string, unknown>[]; labelKey?: string; valueKey?: string; required?: boolean; value?: string; onChange?: (value: string) => void }) {
  const labelKey = props.labelKey ?? "name"; const valueKey = props.valueKey ?? "id";
  return <select name={props.name} required={props.required} value={props.value} onChange={event => props.onChange?.(event.target.value)}><option value="">Select...</option>{props.records.map((row, index) => <option key={String(row[valueKey] ?? index)} value={String(row[valueKey] ?? "")}>{str(row[labelKey] ?? row[valueKey])}</option>)}</select>;
}

export function Status({ value }: { value: unknown }) { const text = str(value, "unknown"); return <span className="vela-status" data-state={statusTone(text)} aria-label={`Status: ${text}`}>{text}</span>; }
function statusTone(value: string) { const state = value.toLowerCase(); if (/complete|completed|finished/.test(state)) return "completed"; if (/success|approved|healthy|active|ready|present/.test(state)) return "success"; if (/fail|error|reject|missing|cancel/.test(state)) return "danger"; if (/queue|pending|running|draft|hold|attention|unknown/.test(state)) return "warning"; return "neutral"; }
