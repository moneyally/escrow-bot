import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Empty, Group, Mono, useUi } from "../ui";
import { STATE, micro } from "../labels";

type Person = {
  tg_id: number; username: string | null; name: string; role: string; accepted: boolean; joined: boolean; notice_ok: boolean;
  choice: string | null; complete: boolean; need_address: boolean;
  slot: { amount_micro: number; status: string; tx_id: string | null } | null;
  payout: { amount_micro: number; address: string | null; status: string } | null;
};
type Deal = { id: number; code: string; state: string; room_id: number | null; fee_micro: number; people: Person[]; split_proposed: boolean; held_micro: number };

const ORDER = ["INVITED", "ROOM", "NOTICE", "CHOOSE", "DEPOSIT", "START_REVIEW", "FUNDED", "SPLIT", "ADDRESS", "PAYOUT"];
const past = (state: string, step: string) => ORDER.indexOf(state) > ORDER.indexOf(step);
const who = (p: Person) => (p.username ? "@" + p.username : p.name || String(p.tg_id));

// 수동 중개: 두 사람이 봇에서 누를 단계를 관리자가 대신. 표 한 장 = 거래 하나 (행 = 단계, 열 = 두 사람)
export function Proxy({ nav, tick }: { nav: Nav; tick: number }) {
  const ui = useUi();
  const [data, setData] = useState<{ enabled: boolean; deals: Deal[] } | null>(null);
  const [form, setForm] = useState({ a: "", b: "" });
  const load = () => api<{ enabled: boolean; deals: Deal[] }>("/api/proxy").then(setData).catch(() => {});
  useEffect(() => { load(); }, [tick]);

  async function act(d: Deal, action: string, p: Person | null, label: string, value?: unknown) {
    const reason = await ui.prompt({ title: label, message: `${d.code}${p ? " · " + who(p) + " 대신" : ""}\n관리자가 대신 처리한 걸로 기록돼요.`,
      confirm: "처리", placeholder: "메모 (선택)" });
    if (reason === null) return;
    try { const r = await post(`/api/proxy/${d.id}/act`, { action, as_tg: p?.tg_id ?? null, value, reason }); ui.toast(r.result); load(); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }
  async function ask(d: Deal, action: string, p: Person, title: string, placeholder: string) {
    const v = await ui.prompt({ title, message: `${d.code} · ${who(p)}`, confirm: "다음", placeholder });
    if (!v) return;
    if (action === "address") {
      const again = await ui.prompt({ title: "주소 한 번 더", message: "잘못 보내면 되돌릴 수 없어요. 똑같이 한 번 더 넣어주세요.", confirm: "확인", placeholder });
      if (again === null) return;
      if (again.trim() !== v.trim()) return ui.toast("두 번 넣은 주소가 달라요.");
    }
    act(d, action, p, title, v.trim());
  }
  async function create() {
    if (!form.a.trim() || !form.b.trim()) return ui.toast("두 사람을 넣어주세요.");
    if (!(await ui.ask({ title: "중개 거래 만들기", message: `${form.a} ↔ ${form.b}\n상대 수락까지 바로 대신 처리돼요.`, confirm: "만들기" }))) return;
    try { const r = await post("/api/proxy/new", { a: form.a, b: form.b }); ui.toast(r.result); setForm({ a: "", b: "" }); load(); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }

  if (!data) return null;
  if (!data.enabled) return <Empty>수동 중개가 꺼져 있어요. 설정에서 켜면 이 화면을 쓸 수 있어요.</Empty>;
  return (
    <>
      <Group title="새 중개 거래" footer="두 사람 모두 이 봇에 /start 를 한 번 보낸 적이 있어야 해요. 진행 메시지는 평소처럼 두 사람에게 가요.">
        <div className="inline pad">
          <input className="field" placeholder="A (신청자) @아이디 또는 숫자 ID" value={form.a} onChange={(e) => setForm({ ...form, a: e.target.value })} />
          <input className="field" placeholder="B (상대) @아이디 또는 숫자 ID" value={form.b} onChange={(e) => setForm({ ...form, b: e.target.value })} />
          <Button onClick={create}>만들기</Button>
        </div>
      </Group>
      {!data.deals.length && <Empty>진행 중인 거래가 없어요.</Empty>}
      {data.deals.map((d) => <DealBoard key={d.id} d={d} nav={nav} act={act} ask={ask} />)}
    </>
  );
}

type ActFn = (d: Deal, action: string, p: Person | null, label: string, value?: unknown) => void;
type AskFn = (d: Deal, action: string, p: Person, title: string, placeholder: string) => void;

function DealBoard({ d, nav, act, ask }: { d: Deal; nav: Nav; act: ActFn; ask: AskFn }) {
  const s = d.state;
  const done = (t = "완료") => <span className="cell-ok">✓ {t}</span>;
  const wait = <span className="cell-wait">—</span>;
  const btn = (label: string, on: () => void, tone: "primary" | "secondary" = "secondary") =>
    <button className={`cell-btn ${tone}`} onClick={on}>{label}</button>;

  const rows: [string, (p: Person) => React.ReactNode][] = [
    ["요청 수락", (p) => p.role === "creator" ? done("신청") : p.accepted || s !== "INVITED" ? done() : btn("대신 수락", () => act(d, "accept", p, "대신 수락"))],
    ["거래방", (p) => s === "ROOM" ? (p.joined ? done("입장") : btn("방 없이 진행", () => act(d, "skip_room", null, "거래방 없이 진행"))) :
      past(s, "ROOM") ? done(d.room_id ? "방" : "1:1") : wait],
    ["공지 확인", (p) => p.notice_ok || past(s, "NOTICE") ? done() : s === "NOTICE" ? btn("대신 확인", () => act(d, "notice", p, "공지 대신 확인")) : wait],
    ["예치 선택", (p) => p.choice ? done(p.choice === "deposit" ? "예치" : "예치 안 함") : s === "CHOOSE" ? (
      <span className="cell-pair">{btn("예치", () => act(d, "choose", p, "예치로 선택", true))}{btn("안 함", () => act(d, "choose", p, "예치 안 함으로 선택", false))}</span>) : wait],
    ["예치 금액", (p) => p.choice !== "deposit" ? wait : p.slot ? <span className="num">{micro(p.slot.amount_micro)}</span> :
      s === "DEPOSIT" ? btn("금액 입력", () => ask(d, "amount", p, "예치 금액", "USDT 예: 1000")) : wait],
    ["입금", (p) => p.choice !== "deposit" ? wait : !p.slot ? wait : ["received", "confirmed"].includes(p.slot.status) ? done(p.slot.status === "confirmed" ? "확인됨" : "들어옴") :
      btn("입금 완료 처리", () => act(d, "deposit", p, `입금 완료 처리 (${micro(p.slot!.amount_micro)} USDT)`), "primary")],
    ["거래완료", (p) => p.complete || past(s, "FUNDED") ? done() : s === "FUNDED" ? btn("대신 완료", () => act(d, "complete", p, "거래완료 대신")) : wait],
    ["받을 주소", (p) => p.payout?.address ? <Mono>{p.payout.address.slice(0, 6)}…{p.payout.address.slice(-4)}</Mono> :
      p.need_address ? btn("주소 입력", () => ask(d, "address", p, "받을 주소 (TRC20)", "T로 시작하는 34자")) : wait],
  ];
  const [a, b] = d.people;
  return (
    <Group title={<span className="board-title"><button className="link" onClick={() => nav.openDeal(d.code)}><Mono>{d.code}</Mono></button>
      <Badge tone={s === "DISPUTE" ? "red" : "blue"}>{STATE[s] || s}</Badge><span className="sub">보관 {micro(d.held_micro)} USDT · 수수료 {micro(d.fee_micro)}</span></span>}>
      <div className="table-wrap">
        <table className="board">
          <thead><tr><th>단계</th><th>{a ? who(a) : "A"} <span className="sub">신청자</span></th><th>{b ? who(b) : "B"} <span className="sub">상대</span></th></tr></thead>
          <tbody>
            {rows.map(([label, cell]) => (
              <tr key={label}><td className="step">{label}</td><td>{a ? cell(a) : wait}</td><td>{b ? cell(b) : wait}</td></tr>
            ))}
            <tr>
              <td className="step">정산 방법</td>
              <td colSpan={2}>{s === "SPLIT" ? (
                <span className="cell-pair">
                  <button className="cell-btn secondary" onClick={() => act(d, "split", null, "각자 예치한 만큼", { how: "each" })}>각자 예치한 만큼</button>
                  <button className="cell-btn secondary" onClick={() => act(d, "split", null, `전액 ${a ? who(a) : "A"} 에게`, { how: "a" })}>전액 {a ? who(a) : "A"}</button>
                  <button className="cell-btn secondary" onClick={() => act(d, "split", null, `전액 ${b ? who(b) : "B"} 에게`, { how: "b" })}>전액 {b ? who(b) : "B"}</button>
                  <button className="cell-btn secondary" onClick={async () => {
                    const v = window.prompt(`${a ? who(a) : "A"} 가 받을 금액 (나머지는 상대)`);
                    if (v) act(d, "split", null, `직접 입력: ${a ? who(a) : "A"} ${v} USDT`, { how: "custom", a: v });
                  }}>직접 입력</button>
                </span>) : past(s, "SPLIT") ? done("정함") : wait}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {["START_REVIEW", "ADDRESS", "PAYOUT"].includes(s) && (
        <p className="group-footer pad-x">다음은 관리자 확인 단계예요 → '확인 대기' 또는 '보낼 것' 화면에서 승인·송금.</p>
      )}
    </Group>
  );
}
