import type { FormEvent, ReactNode } from "react";

export const cx = (...values: Array<string | false | null | undefined>) => values.filter(Boolean).join(" ");

export function Icon({ name, size = 19 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    overview: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
    camera: <><path d="M3 7h13a2 2 0 0 1 2 2v8H3z"/><path d="m18 11 4-2v8l-4-2"/><circle cx="8" cy="12" r="2"/></>,
    radar: <><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="m12 12 6-6M12 2v2M22 12h-2M12 22v-2M2 12h2"/></>,
    boxes: <><path d="m12 2 8 4-8 4-8-4zM4 10l8 4 8-4M4 14l8 4 8-4"/></>,
    cluster: <><rect x="3" y="4" width="18" height="5" rx="1"/><rect x="3" y="15" width="18" height="5" rx="1"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h7M11 17.5h7"/></>,
    bell: <><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/></>,
    pulse: <path d="M3 12h4l2-7 4 14 2-7h6"/>,
    history: <><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"/></>,
    search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
    refresh: <><path d="M20 7v5h-5"/><path d="M4 17v-5h5M6.1 8a7 7 0 0 1 11.2-2.3L20 8M4 16l2.7 2.3A7 7 0 0 0 17.9 16"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    arrow: <path d="m9 18 6-6-6-6"/>,
    close: <path d="M6 6l12 12M18 6 6 18"/>,
    check: <path d="m5 12 4 4L19 6"/>,
    warning: <><path d="M12 3 2.5 20h19z"/><path d="M12 9v4M12 17h.01"/></>,
    eye: <><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12"/><circle cx="12" cy="12" r="2.5"/></>,
    play: <path d="m8 5 11 7-11 7z"/>,
    stop: <rect x="6" y="6" width="12" height="12" rx="1"/>,
  };
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.overview}</svg>;
}

export function StatusPill({ value, label }: { value?: string | boolean | null; label?: string }) {
  const raw = String(value ?? "unknown");
  const normalized = raw.toLowerCase();
  const tone = ["healthy", "online", "ready", "running", "applied", "succeeded", "sent", "active", "ok", "true"].includes(normalized)
    ? "good" : ["degraded", "warning", "pending", "progressing", "applying", "queued", "validating", "acknowledged", "unconfigured"].includes(normalized)
      ? "warn" : ["unavailable", "offline", "failed", "invalid", "critical", "false", "expired"].includes(normalized)
        ? "bad" : "neutral";
  return <span className={cx("status-pill", tone)}><i />{label || raw.replaceAll("_", " ")}</span>;
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return <header className="page-header"><div>{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</header>;
}

export function Panel({ title, subtitle, action, children, className }: { title?: string; subtitle?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={cx("panel", className)}>{(title || action) && <div className="panel-head"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>}<div className="panel-body">{children}</div></section>;
}

export function MetricCard({ label, value, hint, status, icon }: { label: string; value: ReactNode; hint?: string; status?: string; icon?: string }) {
  return <article className="metric-card"><div className="metric-top"><span>{label}</span>{icon && <span className="metric-icon"><Icon name={icon} /></span>}</div><strong>{value}</strong><div className="metric-foot">{status && <StatusPill value={status} />}{hint && <span>{hint}</span>}</div></article>;
}

export function EmptyState({ icon = "boxes", title, description, action }: { icon?: string; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span className="empty-icon"><Icon name={icon} size={24} /></span><h3>{title}</h3><p>{description}</p>{action}</div>;
}

export function SearchBox({ value, onChange, placeholder = "Search" }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return <label className="search-box"><Icon name="search" size={17} /><input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} /></label>;
}

export function Modal({ title, description, children, onClose, wide = false }: { title: string; description?: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section className={cx("modal", wide && "modal-wide")} role="dialog" aria-modal="true" aria-label={title}><header><div><h2>{title}</h2>{description && <p>{description}</p>}</div><button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="close" /></button></header>{children}</section></div>;
}

export function FormActions({ children }: { children: ReactNode }) { return <div className="form-actions">{children}</div>; }
export function Field({ label, children, hint, wide = false }: { label: string; children: ReactNode; hint?: string; wide?: boolean }) { return <label className={cx("field", wide && "field-wide")}><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }

export function JsonView({ value }: { value: unknown }) { return <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>; }

export function ConfirmButton({ children, message, onConfirm, className = "button danger", disabled = false }: { children: ReactNode; message: string; onConfirm: () => void | Promise<void>; className?: string; disabled?: boolean }) {
  return <button className={className} disabled={disabled} onClick={() => window.confirm(message) && void onConfirm()}>{children}</button>;
}

export function submitForm(event: FormEvent<HTMLFormElement>, handler: (data: FormData) => void | Promise<void>) {
  event.preventDefault();
  void handler(new FormData(event.currentTarget));
}
