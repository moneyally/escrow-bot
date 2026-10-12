import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Empty, Group, Mono, Table, copyText, useUi } from "../ui";
import { PAYOUT_KIND } from "../labels";

type P = { id: number; kind: string; code: string; user: string; amount: string; amount_exact: string; fee: string; address: string; tx_id: string | null };

export function Payouts({ nav, tick }: { nav: Nav; tick: number }) {
  const ui = useUi();
  const [todo, setTodo] = useState<P[] | null>(null);
  const [sent, setSent] = useState<P[]>([]);
  const [tx, setTx] = useState<Record<number, string>>({});
  useEffect(() => {
    api<P[]>("/api/payouts?status=approved").then(setTodo).catch(() => {});
    api<P[]>("/api/payouts?status=sent").then(setSent).catch(() => {});
  }, [tick]);

  async function done(p: P) {
    const h = (tx[p.id] || "").trim();
    if (!h) return ui.toast("tx 해시를 넣어주세요");
    if (!(await ui.ask({ title: "송금 완료 처리", message: `${p.amount} USDT → @${p.user}\n이 해시로 완료 처리할까요?`, confirm: "완료 처리" }))) return;
    try { const r = await post(`/api/payouts/${p.id}/done`, { tx_id: h }); ui.toast(r.result); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }

  if (!todo) return null;
  return (
    <>
      <p className="hint">지갑에서 아래 주소로 <b>정확한 금액</b>을 보내면 감시가 알아서 찾아 처리해요. 자동으로 안 잡힐 때만 tx 해시를 넣으세요.</p>
      {!todo.length && <Empty>보낼 게 없어요.</Empty>}
      {todo.map((p) => (
        <Group key={p.id}>
          <div className="item">
            <div className="item-head">
              <b className="amount">{p.amount} USDT</b>
              <Badge tone="blue">{PAYOUT_KIND[p.kind] || p.kind}</Badge>
              <button className="link" onClick={() => nav.openDeal(p.code)}><Mono>{p.code}</Mono></button>
              <span>@{p.user}</span>
              <span className="sub">수수료 {p.fee}</span>
            </div>
            <div className="addr"><Mono>{p.address}</Mono></div>
            <div className="actions left">
              <Button tone="secondary" small onClick={() => copyText(p.address, ui)}>주소 복사</Button>
              <Button tone="secondary" small onClick={() => copyText(p.amount_exact, ui)}>금액 복사</Button>
            </div>
            <div className="inline">
              <input className="field" placeholder="tx 해시 (자동 감지가 안 될 때만)" value={tx[p.id] || ""}
                onChange={(e) => setTx({ ...tx, [p.id]: e.target.value })} />
              <Button onClick={() => done(p)}>완료 처리</Button>
            </div>
          </div>
        </Group>
      ))}
      {sent.length > 0 && (
        <Group title="보냄 · 확인 대기">
          <Table head={["거래", "받는 사람", "금액", "tx"]} rows={sent.map((p) => [p.code, "@" + p.user, p.amount, <Mono>{p.tx_id}</Mono>])} />
        </Group>
      )}
    </>
  );
}
