import { useCallback, useEffect, useState } from "react";
import { api, ApiError, Unauthorized, type Stats } from "./api";
import { UiProvider, useUi } from "./ui";
import { Dashboard } from "./views/Dashboard";
import { Approvals } from "./views/Approvals";
import { Payouts } from "./views/Payouts";
import { Deals, DealDetail } from "./views/Deals";
import { Deposits } from "./views/Deposits";
import { Users } from "./views/Users";
import { Rooms } from "./views/Rooms";
import { Settings } from "./views/Settings";
import { Audit } from "./views/Audit";

export type Nav = { go: (tab: string) => void; openDeal: (code: string) => void; refresh: () => void };

const TABS: [string, string, keyof Stats | null][] = [
  ["dash", "대시보드", null], ["approvals", "확인 대기", "pending_approvals"], ["payouts", "보낼 것", "payouts_to_send"],
  ["deals", "거래", null], ["deposits", "입금 확인", "unmatched_in"], ["disputes", "분쟁", "disputes"],
  ["users", "사용자", null], ["rooms", "거래방", null], ["settings", "설정", null], ["audit", "기록", null],
];
const LIVE = new Set(["dash", "approvals", "payouts", "deposits"]);   // 15초마다 새로고침하는 탭

function readHash() {
  const [tab, code] = location.hash.slice(1).split("/");
  return { tab: TABS.some(([k]) => k === tab) ? tab : "dash", code: code ? decodeURIComponent(code) : null };
}

export function App() {
  return <UiProvider><Shell /></UiProvider>;
}

function Shell() {
  const ui = useUi();
  const [{ tab, code }, setLoc] = useState(readHash);
  const [stats, setStats] = useState<Stats | null>(null);
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const h = () => setLoc(readHash());
    window.addEventListener("hashchange", h);
    return () => window.removeEventListener("hashchange", h);
  }, []);

  const refresh = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    api<Stats>("/api/stats").then((s) => { setStats(s); setAuthed(true); })
      .catch((e) => { if (e instanceof Unauthorized) setAuthed(false); else if (e instanceof ApiError) ui.toast(e.message); });
  }, [tick, tab, code, ui]);

  useEffect(() => {
    if (!LIVE.has(tab) || code) return;
    const id = setInterval(() => { if (!document.querySelector("input:focus, select:focus")) refresh(); }, 15000);
    return () => clearInterval(id);
  }, [tab, code, refresh]);

  const nav: Nav = {
    go: (t) => { location.hash = t; },
    openDeal: (c) => { location.hash = `${tab}/${encodeURIComponent(c)}`; },
    refresh,
  };

  if (authed === false) return <Login />;
  if (authed === null) return <div className="boot">불러오는 중…</div>;

  const title = code ? code : TABS.find(([k]) => k === tab)![1];
  return (
    <div className="shell">
      <aside className="sidebar" aria-label="메뉴">
        <div className="brand">에스크로 관리</div>
        <nav>
          {TABS.map(([k, label, key]) => {
            const n = key && stats ? (stats[key] as number) : 0;
            return (
              <a key={k} href={`#${k}`} className={k === tab ? "on" : ""} aria-current={k === tab ? "page" : undefined}>
                <span>{label}</span>{n > 0 && <span className="count">{n}</span>}
              </a>
            );
          })}
        </nav>
      </aside>
      <main className="content">
        <header className="toolbar">
          {code && <button className="back" aria-label="목록으로" onClick={() => { location.hash = tab; }}>‹</button>}
          <h1>{title}</h1>
        </header>
        <div className="page" key={`${tab}/${code}`}>
          {code ? <DealDetail code={code} nav={nav} tick={tick} /> : <View tab={tab} stats={stats} nav={nav} tick={tick} />}
        </div>
      </main>
    </div>
  );
}

function View({ tab, stats, nav, tick }: { tab: string; stats: Stats | null; nav: Nav; tick: number }) {
  switch (tab) {
    case "approvals": return <Approvals nav={nav} tick={tick} />;
    case "payouts": return <Payouts nav={nav} tick={tick} />;
    case "deals": return <Deals nav={nav} />;
    case "disputes": return <Deals nav={nav} fixedState="DISPUTE" />;
    case "deposits": return <Deposits nav={nav} tick={tick} />;
    case "users": return <Users nav={nav} />;
    case "rooms": return <Rooms nav={nav} tick={tick} />;
    case "settings": return <Settings nav={nav} />;
    case "audit": return <Audit />;
    default: return <Dashboard stats={stats} nav={nav} tick={tick} />;
  }
}

function Login() {
  return (
    <div className="login">
      <div className="login-card">
        <h1>에스크로 관리</h1>
        <p>텔레그램 봇에 <code className="mono">/login</code> 을 보내고, 받은 링크로 들어오세요.</p>
        <p className="sub">링크는 5분 동안 한 번만 쓸 수 있어요.</p>
      </div>
    </div>
  );
}
