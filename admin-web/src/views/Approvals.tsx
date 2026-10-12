import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Empty, Group, Mono, useUi } from "../ui";
import { KIND, STATE, when } from "../labels";

type A = { id: number; kind: string; status: string; code: string | null; deal_state: string | null; detail: string; created_at: string };

export function Approvals({ nav, tick }: { nav: Nav; tick: number }) {
  const ui = useUi();
  const [rows, setRows] = useState<A[] | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});
  useEffect(() => { api<A[]>("/api/approvals").then(setRows).catch(() => {}); }, [tick]);

  async function decide(a: A, ok: boolean) {
    if (ok && !(await ui.ask({ title: `${KIND[a.kind] || a.kind} 승인`, message: "돈이 움직이는 단계일 수 있어요. 승인할까요?", confirm: "승인" }))) return;
    try {
      const r = await post(`/api/approvals/${a.id}/${ok ? "approve" : "hold"}`, { note: notes[a.id] || "" });
      ui.toast(r.result); nav.refresh();
    } catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }

  if (!rows) return null;
  if (!rows.length) return <Empty>확인할 게 없어요.</Empty>;
  return (
    <>
      {rows.map((a) => (
        <Group key={a.id} tone={a.status === "held" ? "warn" : undefined}>
          <div className="item">
            <div className="item-head">
              <b>{KIND[a.kind] || a.kind}</b>
              {a.code && <Mono>{a.code}</Mono>}
              {a.deal_state && <Badge>{STATE[a.deal_state] || a.deal_state}</Badge>}
              {a.status === "held" && <Badge tone="orange">보류됨</Badge>}
              <span className="sub">{when(a.created_at)}</span>
            </div>
            <p className="item-text">{a.detail}</p>
            <input className="field" placeholder="메모 (선택)" value={notes[a.id] || ""}
              onChange={(e) => setNotes({ ...notes, [a.id]: e.target.value })} />
            <div className="actions">
              {a.code && <Button tone="plain" onClick={() => nav.openDeal(a.code!)}>거래 보기</Button>}
              {a.status === "pending" && <Button tone="secondary" onClick={() => decide(a, false)}>보류</Button>}
              <Button onClick={() => decide(a, true)}>승인</Button>
            </div>
          </div>
        </Group>
      ))}
    </>
  );
}
