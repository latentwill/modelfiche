import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type RefObject } from "react";
import { Check, ChevronDown, ChevronLeft, ChevronRight, Clipboard, LoaderCircle, UserRound } from "lucide-react";
import { api, idOf, listOf, str } from "./api";
import { useResource } from "./hooks";

export function Page(props: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  return <main className="vela-main"><header className="vela-page-header"><div><h1 id="vela-route-heading">{props.title}</h1>{props.subtitle && <p>{props.subtitle}</p>}</div>{props.actions && <div className="vela-page-actions">{props.actions}</div>}<img className="mf-observation-plate mf-observation-plate-light" src="/assets/microfiche-header-ornament.webp" alt="" aria-hidden="true" /><img className="mf-observation-plate mf-observation-plate-dark" src="/assets/dark-astronomy-header-ornament.webp" alt="" aria-hidden="true" /></header>{props.children}</main>;
}

export function Panel(props: { title?: string; children: ReactNode; className?: string }) {
  return <section className={`vela-panel panel ${props.className ?? ""}`}>{props.title && <div className="vela-section-heading"><h2>{props.title}</h2></div>}{props.children}</section>;
}

export function Notice({ error, loading, empty, emptyText, emptyHint }: { error?: string; loading?: boolean; empty?: boolean; emptyText?: string; emptyHint?: string }) {
  if (error) return <div role="alert" className="vela-notice vela-notice-error"><strong>Unable to load</strong><span>{error}</span></div>;
  if (loading) return <div className="vela-skeleton" role="status" aria-live="polite" aria-busy="true" aria-label="Loading"><span /><span /><span /></div>;
  if (empty) return <div className="vela-empty" role="status" aria-live="polite"><span>{emptyText ?? "No records yet"}</span><small>{emptyHint ?? "This area will update when matching work is available."}</small></div>;
  return null;
}

export type FocusWorkspaceOptions = {
  active: boolean;
  onDismiss: () => void;
  containerRef: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  restoreFocusRef?: RefObject<HTMLElement | null>;
};

const FOCUSABLE_SELECTOR = [
  "a[href]", "button:not([disabled])", "input:not([disabled])", "select:not([disabled])",
  "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
].join(",");

export function useFocusWorkspace(options: FocusWorkspaceOptions): void {
  const dismissRef = useRef(options.onDismiss);
  dismissRef.current = options.onDismiss;
  const closing = useRef(false);
  const previousFocus = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const container = options.containerRef.current;
    if (!options.active || !container) return;
    closing.current = false;
    const isRendered = (element: HTMLElement) => {
      if (element.hidden || element.getAttribute("aria-hidden") === "true") return false;
      let current: HTMLElement | null = element;
      while (current && current !== container.parentElement) {
        const style = window.getComputedStyle(current);
        if (style.display === "none" || style.visibility === "hidden") return false;
        current = current.parentElement;
      }
      return true;
    };
    const focusables = () => Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(isRendered);
    const restoreTarget = options.restoreFocusRef?.current ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    previousFocus.current = restoreTarget;
    const parent = container.parentElement;
    const siblings = parent ? Array.from(parent.children).filter(node => node !== container) as HTMLElement[] : [];
    const prior = siblings.map(element => ({ element, inert: Boolean((element as HTMLElement & { inert?: boolean }).inert), hidden: element.getAttribute("aria-hidden") }));
    for (const element of siblings) {
      (element as HTMLElement & { inert?: boolean }).inert = true;
      element.setAttribute("aria-hidden", "true");
    }
    const initial = options.initialFocusRef?.current;
    const focusTarget = initial && isRendered(initial) ? initial : focusables()[0] ?? container;
    if (!focusTarget.hasAttribute("tabindex") && focusTarget === container) container.setAttribute("tabindex", "-1");
    focusTarget.focus({ preventScroll: true });
    const onFocusIn = (event: FocusEvent) => {
      if (closing.current || container.contains(event.target as Node)) return;
      const first = focusables()[0] ?? container;
      event.stopPropagation();
      first.focus({ preventScroll: true });
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closing.current = true;
        dismissRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const available = focusables();
      if (!available.length) {
        event.preventDefault();
        container.focus({ preventScroll: true });
        return;
      }
      const first = available[0];
      const last = available[available.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    container.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      container.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      for (const { element, inert, hidden } of prior) {
        (element as HTMLElement & { inert?: boolean }).inert = inert;
        if (hidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", hidden);
      }
      if (restoreTarget && restoreTarget.isConnected) restoreTarget.focus({ preventScroll: true });
      previousFocus.current = null;
      closing.current = false;
    };
  }, [options.active, options.containerRef, options.initialFocusRef, options.restoreFocusRef]);
}

export type DropdownOption = {
  value: string;
  label: string;
  description?: string;
  disabled?: boolean;
};

export type DropdownProps = {
  options: DropdownOption[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  name?: string;
  required?: boolean;
  disabled?: boolean;
  placeholder?: string;
  id?: string;
  className?: string;
  "aria-label"?: string;
};

function nextEnabledOption(options: DropdownOption[], current: number, direction: -1 | 1) {
  const start = current >= 0 && current < options.length ? current : direction === 1 ? -1 : 0;
  for (let offset = 1; offset <= options.length; offset += 1) {
    const index = (start + direction * offset + options.length) % options.length;
    if (!options[index].disabled) return index;
  }
  return -1;
}

export function Dropdown({
  options,
  value,
  defaultValue = "",
  onChange,
  name,
  required = false,
  disabled = false,
  placeholder = "Select...",
  id,
  className,
  "aria-label": ariaLabel,
}: DropdownProps) {
  const generatedId = useId();
  const buttonId = id ?? `${generatedId}-button`;
  const listboxId = `${generatedId}-listbox`;
  const validationId = `${generatedId}-validation`;
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<Array<HTMLDivElement | null>>([]);
  const [internalValue, setInternalValue] = useState(defaultValue);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [invalid, setInvalid] = useState(false);
  const selectedValue = value === undefined ? internalValue : value;
  const selectedIndex = options.findIndex(option => option.value === selectedValue);
  const selectedOption = options[selectedIndex];

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const positionMenu = () => {
      const button = buttonRef.current;
      const menu = menuRef.current;
      if (!button || !menu) return;

      const anchor = button.getBoundingClientRect();
      const theme = getComputedStyle(rootRef.current ?? button);
      const gap = Number.parseFloat(theme.getPropertyValue("--vela-space-1")) || 0;
      const inset = Number.parseFloat(theme.getPropertyValue("--vela-space-2")) || 0;
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
      menu.style.setProperty("--mf-dropdown-anchor-width", `${anchor.width}px`);

      const menuHeight = menu.getBoundingClientRect().height;
      const spaceBelow = Math.max(0, viewportHeight - anchor.bottom - gap - inset);
      const spaceAbove = Math.max(0, anchor.top - gap - inset);
      const openAbove = menuHeight > spaceBelow && spaceAbove > spaceBelow;
      menu.style.setProperty("--mf-dropdown-available-height", `${openAbove ? spaceAbove : spaceBelow}px`);

      const menuRect = menu.getBoundingClientRect();
      const left = Math.min(Math.max(anchor.left, inset), Math.max(inset, viewportWidth - inset - menuRect.width));
      menu.style.left = `${left}px`;
      menu.style.top = `${openAbove ? Math.max(inset, anchor.top - gap - menuRect.height) : anchor.bottom + gap}px`;
    };

    positionMenu();
    window.addEventListener("resize", positionMenu);
    document.addEventListener("scroll", positionMenu, true);
    return () => {
      window.removeEventListener("resize", positionMenu);
      document.removeEventListener("scroll", positionMenu, true);
    };
  }, [open, options]);

  useEffect(() => {
    if (open && activeIndex >= 0) optionRefs.current[activeIndex]?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, open]);

  useEffect(() => {
    const form = rootRef.current?.closest("form");
    if (!form) return;
    const handleSubmit = (event: SubmitEvent) => {
      if (!required || disabled || selectedValue) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      setInvalid(true);
      buttonRef.current?.focus();
    };
    const handleReset = () => {
      if (value === undefined) setInternalValue(defaultValue);
      setInvalid(false);
      setOpen(false);
    };
    form.addEventListener("submit", handleSubmit, true);
    form.addEventListener("reset", handleReset);
    return () => {
      form.removeEventListener("submit", handleSubmit, true);
      form.removeEventListener("reset", handleReset);
    };
  }, [defaultValue, disabled, required, selectedValue, value]);

  const openAt = (direction: -1 | 1 = 1) => {
    setActiveIndex(selectedIndex >= 0 && !selectedOption.disabled
      ? selectedIndex
      : nextEnabledOption(options, direction === 1 ? -1 : 0, direction));
    setOpen(true);
  };
  const close = (returnFocus = false) => {
    setOpen(false);
    if (returnFocus) buttonRef.current?.focus();
  };
  const select = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    if (value === undefined) setInternalValue(option.value);
    setInvalid(false);
    onChange?.(option.value);
    close(true);
  };

  return <div
    className={`vela-dropdown${className ? ` ${className}` : ""}`}
    ref={rootRef}
    onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close();
    }}
  >
    {name && <input type="hidden" name={name} value={selectedValue} disabled={disabled} readOnly />}
    <button
      id={buttonId}
      ref={buttonRef}
      className={`vela-dropdown-button${selectedOption ? "" : " is-placeholder"}`}
      type="button"
      aria-haspopup="listbox"
      disabled={disabled}
      aria-label={ariaLabel}
      aria-describedby={invalid ? validationId : undefined}
      aria-expanded={open}
      aria-controls={listboxId}
      aria-activedescendant={open && options[activeIndex] ? `${listboxId}-option-${activeIndex}` : undefined}
      aria-required={required || undefined}
      aria-invalid={invalid || undefined}
      onClick={() => open ? close() : openAt()}
      onKeyDown={event => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const direction = event.key === "ArrowDown" ? 1 : -1;
          open ? setActiveIndex(current => nextEnabledOption(options, current, direction)) : openAt(direction);
        } else if (event.key === "Home" || event.key === "End") {
          event.preventDefault();
          const direction = event.key === "Home" ? 1 : -1;
          if (!open) setOpen(true);
          setActiveIndex(nextEnabledOption(options, direction === 1 ? -1 : 0, direction));
        } else if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (open) select(activeIndex);
          else openAt();
        } else if (event.key === "Escape" && open) {
          event.preventDefault();
          close(true);
        } else if (event.key === "Tab") {
          close();
        }
      }}
    >
      <span className="vela-dropdown-value">{selectedOption?.label ?? placeholder}</span>
      <ChevronDown size={15} aria-hidden="true" />
    </button>
    {invalid && <span className="sr-only" id={validationId} role="alert">Select an option.</span>}
    {open && <div
      ref={menuRef}
      className="vela-dropdown-menu"
      id={listboxId}
      role="listbox"
      aria-label={ariaLabel}
      aria-labelledby={ariaLabel ? undefined : buttonId}
    >
      {options.map((option, index) => {
        const optionId = `${listboxId}-option-${index}`;
        return <div
          id={optionId}
          ref={node => { optionRefs.current[index] = node; }}
          key={`${option.value}-${index}`}
          className="vela-dropdown-option"
          role="option"
          aria-selected={option.value === selectedValue}
          aria-disabled={option.disabled || undefined}
          aria-labelledby={`${optionId}-label`}
          aria-describedby={option.description ? `${optionId}-description` : undefined}
          data-active={index === activeIndex || undefined}
          onPointerDown={event => event.preventDefault()}
          onPointerMove={() => { if (!option.disabled) setActiveIndex(index); }}
          onClick={() => select(index)}
        >
          <span className="vela-dropdown-option-copy">
            <strong id={`${optionId}-label`}>{option.label}</strong>
            {option.description && <small id={`${optionId}-description`}>{option.description}</small>}
          </span>
          {option.value === selectedValue && <Check size={14} aria-hidden="true" />}
        </div>;
      })}
    </div>}
  </div>;
}

export function ProfileSelect({ compact = false, workspaceName = "" }: { compact?: boolean; workspaceName?: string }) {
  const resource = useResource<unknown>("/api/profiles");
  const serverActive = useResource<Record<string, unknown>>("/api/profiles/active");
  const profiles = listOf<Record<string, unknown>>(resource.data).filter(profile => profile.is_active !== false);
  const [active, setActive] = useState(() => localStorage.getItem("titles.activeProfileId") ?? "");
  const [error, setError] = useState("");
  const controlId = useId();
  const label = `Active profile${workspaceName ? ` in ${workspaceName}` : ""}`;
  useEffect(() => { if (!active && serverActive.data?.id) setActive(idOf(serverActive.data)); }, [active, serverActive.data]);
  return <div className={`vela-profile-select ${compact ? "is-compact" : ""}`} title={error || label}>
    <UserRound size={16} aria-hidden="true" />
    <label className={compact ? "sr-only" : ""} htmlFor={controlId}>{label}</label>
    <Dropdown
      id={controlId}
      aria-label={label}
      value={active}
      options={[
        { value: "", label: workspaceName ? `${workspaceName} profile` : "Profile" },
        ...profiles.map(profile => ({ value: idOf(profile), label: str(profile.display_name ?? profile.name) })),
      ]}
      onChange={async profileId => {
        const previous = active;
        setActive(profileId);
        setError("");
        try {
          if (profileId) {
            await api(`/api/profiles/${profileId}/active`, { method: "PUT" });
            localStorage.setItem("titles.activeProfileId", profileId);
          } else {
            localStorage.removeItem("titles.activeProfileId");
          }
          location.reload();
        } catch (reason) {
          setActive(previous);
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      }}
    />
    {error && <span className="sr-only" role="alert">{error}</span>}
  </div>;
}

export function Field(props: { label: string; children: ReactNode; hint?: string }) {
  return <label className="vela-field field"><span>{props.label}</span>{props.children}{props.hint && <small>{props.hint}</small>}</label>;
}

export function Form(props: { onSubmit: (form: FormData) => void | Promise<void>; children: ReactNode; submit?: string; submitDisabled?: boolean; repeatable?: boolean }) {
  const [error, setError] = useState(""); const [saving, setSaving] = useState(false); const [saved, setSaved] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); setError(""); setSaved(false); setSaving(true); try { await props.onSubmit(new FormData(event.currentTarget)); setSaved(true); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setSaving(false); } };
  return <form className="vela-form" onSubmit={event => void submit(event)}>{props.children}{error && <div role="alert" className="vela-notice vela-notice-error">{error}</div>}<button className="vela-button vela-button-primary" type="submit" disabled={saving || props.submitDisabled}>{saving ? <><LoaderCircle className="vela-spin" size={15} /> Working...</> : saved && !props.repeatable ? <><Check size={15} /> Saved</> : props.submit ?? "Save"}</button></form>;
}

export function Json({ value }: { value: unknown }) { return <pre className="vela-code">{JSON.stringify(value, null, 2)}</pre>; }

export function Pagination({ page, pageSize, total, onPage, onPageSize }: { page: number; pageSize: number; total: number; onPage: (page: number) => void; onPageSize: (size: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const start = total ? (page - 1) * pageSize + 1 : 0;
  const end = Math.min(page * pageSize, total);
  return <nav className="vela-pagination" aria-label="Pagination">
    <span>{start}–{end} of {total}</span>
    <label>Per page <Dropdown aria-label="Items per page" value={String(pageSize)} options={[50, 100, 250].map(size => ({ value: String(size), label: String(size) }))} onChange={next => onPageSize(Number(next))} /></label>
    <button aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft size={15} /></button>
    <span>Page {page} of {pages}</span>
    <button aria-label="Next page" disabled={page >= pages} onClick={() => onPage(page + 1)}><ChevronRight size={15} /></button>
  </nav>;
}

export function DataTable(props: {
  rows: Record<string, unknown>[];
  columns: Array<[string, string]>;
  onRow?: (row: Record<string, unknown>) => void;
  total?: number;
  page?: number;
  pageSize?: number;
  onPageChange?: (page: number) => void;
}) {
  const hasServerPagination = props.total !== undefined && props.pageSize !== undefined && props.onPageChange;
  const page = Math.max(1, props.page ?? 1);
  const pageSize = Math.max(1, props.pageSize ?? 1);
  const total = Math.max(0, props.total ?? 0);
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const start = total ? (page - 1) * pageSize + 1 : 0;
  const end = Math.min(page * pageSize, total);
  return <div className="vela-table-wrap table-wrap" role="region" aria-label="Scrollable data table" tabIndex={0}>
    <p className="vela-table-scroll-hint">Scroll horizontally for all columns</p>
    <table className="vela-table"><thead><tr>{props.columns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>{props.rows.map((row, index) => {
      return <tr key={String(row.id ?? index)}>{props.columns.map(([key], columnIndex) => <td key={key}>{columnIndex === 0 && props.onRow ? <button type="button" className="vela-table-row-action" onClick={() => props.onRow?.(row)}>{renderCell(row[key])}</button> : renderCell(row[key])}</td>)}</tr>;
    })}</tbody></table>
    {hasServerPagination && <nav className="vela-pagination" aria-label="Table pagination">
      <span>{start}–{end} of {total}</span>
      <button aria-label="Previous page" disabled={page <= 1} onClick={() => props.onPageChange?.(page - 1)}><ChevronLeft size={15} /></button>
      <span>Page {page} of {pages}</span>
      <button aria-label="Next page" disabled={page >= pages} onClick={() => props.onPageChange?.(page + 1)}><ChevronRight size={15} /></button>
    </nav>}
  </div>;
}

function renderCell(value: unknown): ReactNode { if (typeof value === "boolean") return value ? "Yes" : "No"; if (Array.isArray(value)) return value.map(String).join(", "); if (value && typeof value === "object") return JSON.stringify(value); if (typeof value === "string" && /^https?:\/\//.test(value)) return <a href={value} target="_blank" rel="noreferrer" onClick={event => event.stopPropagation()}>Open</a>; return str(value); }

export function SelectRecords(props: { name: string; records: Record<string, unknown>[]; labelKey?: string; valueKey?: string; required?: boolean; disabled?: boolean; value?: string; defaultValue?: string; onChange?: (value: string) => void }) {
  const labelKey = props.labelKey ?? "name";
  const valueKey = props.valueKey ?? "id";
  return <Dropdown
    name={props.name}
    required={props.required}
    disabled={props.disabled}
    value={props.value}
    defaultValue={props.defaultValue}
    options={[
      { value: "", label: "Select..." },
      ...props.records.map(row => ({ value: String(row[valueKey] ?? ""), label: str(row[labelKey] ?? row[valueKey]) })),
    ]}
    onChange={props.onChange}
  />;
}

type TextCaret = { node: Node; offset: number };

function textCaretAtPoint(x: number, y: number): TextCaret | null {
  const position = document.caretPositionFromPoint?.(x, y);
  if (position) return { node: position.offsetNode, offset: position.offset };
  const range = (document as Document & { caretRangeFromPoint?: (clientX: number, clientY: number) => Range | null }).caretRangeFromPoint?.(x, y);
  return range ? { node: range.startContainer, offset: range.startOffset } : null;
}

export function useDragTextSelection() {
  const anchor = useRef<(TextCaret & { pointerId: number }) | null>(null);
  const update = (focus: TextCaret) => {
    const start = anchor.current;
    const selection = window.getSelection();
    if (!start || !selection) return;
    const range = document.createRange();
    range.setStart(start.node, start.offset);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    selection.extend(focus.node, focus.offset);
  };
  const finish = (event: ReactPointerEvent<HTMLElement>) => {
    if (anchor.current?.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    anchor.current = null;
  };
  return {
    onPointerDown: (event: ReactPointerEvent<HTMLElement>) => {
      const target = event.target instanceof Element ? event.target : null;
      if (event.button !== 0 || !target?.closest("dt, dd, p, pre, code, h2, h3") || target.closest("button, a, input, textarea, summary")) return;
      const caret = textCaretAtPoint(event.clientX, event.clientY);
      if (!caret) return;
      event.preventDefault();
      anchor.current = { ...caret, pointerId: event.pointerId };
      event.currentTarget.setPointerCapture(event.pointerId);
      update(caret);
    },
    onPointerMove: (event: ReactPointerEvent<HTMLElement>) => {
      if (anchor.current?.pointerId !== event.pointerId) return;
      const caret = textCaretAtPoint(event.clientX, event.clientY);
      if (caret) update(caret);
    },
    onPointerUp: finish,
    onPointerCancel: finish,
  };
}

function metadataClipboardText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value ?? "");
  } catch {
    return String(value ?? "");
  }
}

export function CopyMetadataButton({ value }: { value: unknown }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => setState("idle"), [value]);
  return <button
    type="button"
    className="vela-text-button metadata-copy-button"
    onClick={async () => {
      try {
        await navigator.clipboard.writeText(metadataClipboardText(value));
        setState("copied");
      } catch {
        setState("failed");
      }
    }}
  >
    {state === "copied" ? <Check size={13} /> : <Clipboard size={13} />}
    {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : "JSON"}
  </button>;
}

export function Status({ value }: { value: unknown }) { const text = str(value, "unknown"); return <span className="vela-status" data-state={statusTone(text)} aria-label={`Status: ${text}`}>{text}</span>; }
function statusTone(value: string) { const state = value.toLowerCase(); if (/complete|completed|finished/.test(state)) return "completed"; if (/success|approved|healthy|active|ready|present/.test(state)) return "success"; if (/fail|error|reject|missing|cancel/.test(state)) return "danger"; if (/queue|pending|running|draft|hold|attention|unknown/.test(state)) return "warning"; return "neutral"; }
