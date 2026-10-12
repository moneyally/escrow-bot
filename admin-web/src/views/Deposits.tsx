import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Empty, Group, Mono, Table, useUi } from "../ui";
import { when } from "../labels";

type T = { tx_id: string; idx: number; direction: string; amount: string; status: string; note: string; from_addr: string; to_addr: string; block_ts: string };
type S = { id: number; code: string; user: string; amount: string; status: string };

export function Deposits({ nav, tick }: { nav: Nav; tick: number }) {
  const ui = useUi();
  const [open, setOpen] = useState<T[] | null>(null);
  const [all, setAll] = useState<T[]>([]);
  const [slots, setSlots] = useState<S[]>([]);
  const [pick, setPick] = useState<Record<string, string>>({});
  useEffect(() => {
    api<T[]>("/api/deposits?status=unmatched").then(setOpen).catch(() => {});
    api<T[]>("/api/deposits").then(setAll).catch(() => {});
    api<S[]>("/api/slots/open").then(setSlots).catch(() => {});
  }, [tick]);

  async function run(path: string, body: unknown) {
    try { const r = await post(path, body); ui.toast(r.result); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }
  async function assign(t: T) {
    const id = pick[t.tx_id + t.idx];
    if (!id) return ui.toast("거래 자리를 골라주세요");
    if (await ui.ask({ title: "입금 연결", message: `${t.amount} USDT 를 그 거래에 연결할까요? 받은 금액 그대로 인정돼요.`, confirm: "연결" }))
      run(`/api/deposits/${t.tx_id}/${t.idx}/assign`, { slot_id: Number(id) });
  }
  async function mark(t: T, status: "returned" | "ignored") {
    const note = await ui.prompt({
      title: status === "returned" ? "돌려줌 표시" : "무시", confirm: "표시",
      placeholder: status === "returned" ? "돌려준 tx 해시나 메모" : "무시하는 이유",
    });
    if (note !== null) run(`/api/deposits/${t.tx_id}/${t.idx}/mark`, { status, note });
  }

  if (!open) return null;
  return (
    <>
      {!open.length && <Empty>확인 필요한 입금이 없어요.</Empty>}
      {open.map((t) => (
        <Group key={t.tx_id + t.idx} tone={t.status === "flagged" ? "bad" : "warn"}>
          <div className="item">
            <div className="item-head">
              <b className="amount">{t.amount} USDT</b>
              <Badge tone={t.status === "flagged" ? "red" : "orange"}>{t.status === "flagged" ? "동결 주소" : "맞는 거래 없음"}</Badge>
              <span className="sub">{when(t.block_ts)}</span>
            </div>
            {t.note && <p className="item-text">{t.note}</p>}
            <p className="sub">보낸 곳 <Mono>{t.from_addr}</Mono></p>
            <p className="sub">tx <a href={`https://tronscan.org/#/transaction/${encodeURIComponent(t.tx_id)}`} target="_blank" rel="noopener noreferrer"><Mono>{t.tx_id}</Mono></a></p>
            <div className="inline">
              <select className="field" value={pick[t.tx_id + t.idx] || ""} onChange={(e) => setPick({ ...pick, [t.tx_id + t.idx]: e.target.value })} aria-label="거래 자리">
                <option value="">거래 자리 고르기</option>
                {slots.map((s) => <option key={s.id} value={s.id}>{s.code} @{s.user} {s.amount} ({s.status === "waiting" ? "대기" : "만료"})</option>)}
              </select>
              <Button onClick={() => assign(t)}>연결</Button>
            </div>
            <div className="actions left">
              <Button tone="secondary" small onClick={() => mark(t, "returned")}>돌려줌 표시</Button>
              <Button tone="plain" small onClick={() => mark(t, "ignored")}>무시</Button>
            </div>
          </div>
        </Group>
      ))}
      <Group title="최근 체인 기록">
        <Table head={["방향", "금액", "상태", "보낸 곳 → 받는 곳", "시각"]}
          rows={all.slice(0, 50).map((t) => [t.direction === "in" ? "입금" : "출금", t.amount, t.status,
            <Mono>{t.from_addr} → {t.to_addr}</Mono>, when(t.block_ts)])} />
      </Group>
    </>
  );
}
