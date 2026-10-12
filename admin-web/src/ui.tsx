import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

// ── 버튼: 캡슐, 44px, 누르면 살짝 작아짐 ──
type Tone = "primary" | "secondary" | "destructive" | "plain";
export function Button({ tone = "primary", small, children, ...p }:
  { tone?: Tone; small?: boolean } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button {...p} className={`btn btn-${tone}${small ? " btn-sm" : ""} ${p.className || ""}`}>{children}</button>;
}

// ── 묶음 목록 (설정 앱 같은 inset grouped) ──
export function Group({ title, footer, children, tone }: { title?: ReactNode; footer?: ReactNode; children: ReactNode; tone?: "warn" | "bad" }) {
  return (
    <section className="group">
      {title && <h3 className="group-title">{title}</h3>}
      <div className={`group-body${tone ? " tone-" + tone : ""}`}>{children}</div>
      {footer && <p className="group-footer">{footer}</p>}
    </section>
  );
}

export function Row({ label, detail, children, onClick, chevron }:
  { label?: ReactNode; detail?: ReactNode; children?: ReactNode; onClick?: () => void; chevron?: boolean }) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag className={`row${onClick ? " row-tap" : ""}`} onClick={onClick}>
      <div className="row-main">
        {label != null && <div className="row-label">{label}</div>}
        {detail != null && <div className="row-detail">{detail}</div>}
      </div>
      {children != null && <div className="row-trail">{children}</div>}
      {chevron && <span className="chevron" aria-hidden>›</span>}
    </Tag>
  );
}

export function Badge({ children, tone = "gray" }: { children: ReactNode; tone?: "gray" | "blue" | "green" | "orange" | "red" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  if (!rows.length) return <Empty>없어요</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="mono">{children}</code>;
}

// ── 알림창(alert) · 토스트 ──
type AlertReq = {
  title: string; message?: string; confirm: string; destructive?: boolean;
  input?: { placeholder: string }; resolve: (v: string | null) => void;
};
type Ui = {
  toast: (m: string) => void;
  ask: (o: { title: string; message?: string; confirm: string; destructive?: boolean }) => Promise<boolean>;
  prompt: (o: { title: string; message?: string; confirm: string; placeholder: string }) => Promise<string | null>;
};
const UiCtx = createContext<Ui>(null!);
export const useUi = () => useContext(UiCtx);

export function UiProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [alert, setAlert] = useState<AlertReq | null>(null);
  const timer = useRef<number>(0);
  const toast = useCallback((m: string) => {
    setMsg(m);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMsg(null), 3200);
  }, []);
  const ask: Ui["ask"] = useCallback((o) => new Promise((res) =>
    setAlert({ ...o, resolve: (v) => res(v !== null) })), []);
  const prompt: Ui["prompt"] = useCallback((o) => new Promise((res) =>
    setAlert({ ...o, input: { placeholder: o.placeholder }, resolve: res })), []);
  return (
    <UiCtx.Provider value={{ toast, ask, prompt }}>
      {children}
      {alert && <AlertView a={alert} close={(v) => { setAlert(null); alert.resolve(v); }} />}
      <div className={`toast${msg ? " show" : ""}`} role="status" aria-live="polite">{msg}</div>
    </UiCtx.Provider>
  );
}

function AlertView({ a, close }: { a: AlertReq; close: (v: string | null) => void }) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    ref.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && close(null);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [close]);
  return (
    <div className="backdrop" onClick={() => close(null)}>
      <div className="alert" role="alertdialog" aria-modal aria-label={a.title} onClick={(e) => e.stopPropagation()}>
        <div className="alert-body">
          <h4>{a.title}</h4>
          {a.message && <p>{a.message}</p>}
          {a.input && <input ref={ref} className="field" placeholder={a.input.placeholder} value={text}
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && close(text)} />}
        </div>
        <div className="alert-actions">
          <button onClick={() => close(null)}>취소</button>
          <button className={a.destructive ? "destructive" : "strong"} onClick={() => close(a.input ? text : "")}>{a.confirm}</button>
        </div>
      </div>
    </div>
  );
}

// 클립보드 복사 (안 되면 알림창에 보여 줌)
export async function copyText(t: string, ui: Ui) {
  try { await navigator.clipboard.writeText(t); ui.toast("복사했어요"); }
  catch { ui.toast(t); }
}
