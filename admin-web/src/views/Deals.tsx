import { useCallback, useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Empty, Group, Mono, Row, Table, useUi } from "../ui";
import { STATE, micro, when } from "../labels";

type D = { code: string; state: string; creator: string; partner: string | null; created_at: string };

const stateTone = (s: string) =>
  s === "DISPUTE" ? "red" : s === "DONE" ? "green" : s === "CANCELLED" ? "gray" : ["REFUND", "PAYOUT", "START_REVIEW"].includes(s) ? "orange" : "blue";

export function Deals({ nav, fixedState }: { nav: Nav; fixedState?: string }) {
  const [state, setState] = useState(fixedState ?? "active");
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<D[] | null>(null);
  const load = useCallback(() => {
    api<D[]>(`/api/deals?state=${encodeURIComponent(state)}&q=${encodeURIComponent(q.trim())}`).then(setRows).catch(() => {});
  }, [state, q]);
  useEffect(load, [state]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      {!fixedState && (
        <form className="filters" onSubmit={(e) => { e.preventDefault(); load(); }}>
          <select className="field" value={state} onChange={(e) => setState(e.target.value)} aria-label="상태">
            <option value="active">진행 중</option>
            <option value="">전체</option>
            {Object.entries(STATE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <input className="field" type="search" placeholder="거래코드 또는 @아이디" value={q} onChange={(e) => setQ(e.target.value)} />
          <Button type="submit">찾기</Button>
        </form>
      )}
      {rows && !rows.length && <Empty>{fixedState ? "분쟁 중인 거래가 없어요." : "거래가 없어요."}</Empty>}
      {rows && rows.length > 0 && (
        <Group>
          {rows.map((d) => (
            <Row key={d.code} chevron onClick={() => nav.openDeal(d.code)}
              label={<><Mono>{d.code}</Mono> <Badge tone={stateTone(d.state)}>{STATE[d.state] || d.state}</Badge></>}
              detail={`@${d.creator}${d.partner ? " · @" + d.partner : ""} · ${when(d.created_at)}`} />
          ))}
        </Group>
      )}
    </>
  );
}

type Party = { tg_id: number; username: string | null; role: string; notice_ok: boolean; choice: string | null; complete: boolean };
type Detail = {
  deal: { code: string; state: string; created_at: string; fee_micro: number; close_reason: string | null };
  parties: Party[]; balances: Record<string, string>;
  slots: { tg_id: number; amount_micro: number; status: string; tx_id: string | null }[];
  payouts: { tg_id: number; kind: string; amount_micro: number; fee_micro: number; address: string | null; status: string; tx_id: string | null }[];
  events: { at: string; actor: number | null; kind: string; data: unknown }[];
  messages: { id: number; tg_id: number | null; username: string | null; source: string; kind: string; text: string; file_id: string | null; created_at: string }[];
  ratings: { rater: number; stars: number }[];
};

export function DealDetail({ code, nav, tick }: { code: string; nav: Nav; tick: number }) {
  const ui = useUi();
  const [d, setD] = useState<Detail | null>(null);
  const [plan, setPlan] = useState<Record<string, string>>({});
  const [fee, setFee] = useState("");
  const [reason, setReason] = useState("");
  const load = useCallback(() => {
    api<Detail>("/api/deals/" + encodeURIComponent(code)).then(setD).catch((e) => e instanceof ApiError && ui.toast(e.message));
  }, [code, ui]);
  useEffect(load, [load, tick]);

  async function act(path: string, body: unknown, ask: { title: string; message: string; confirm: string; destructive?: boolean }) {
    if (!(await ui.ask(ask))) return;
    try { const r = await post(path, body); ui.toast(r.result); load(); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }

  if (!d) return null;
  const name = (id: number | null) => {
    if (id == null) return "시스템";
    const p = d.parties.find((x) => x.tg_id === id);
    return p ? "@" + (p.username || p.tg_id) : String(id);
  };
  const open = !["DONE", "CANCELLED"].includes(d.deal.state);
  const base = `/api/deals/${encodeURIComponent(code)}`;

  return (
    <>
      <Group>
        <Row label="상태"><Badge tone={stateTone(d.deal.state)}>{STATE[d.deal.state] || d.deal.state}</Badge></Row>
        <Row label="만든 때">{when(d.deal.created_at)}</Row>
        <Row label="수수료"><span className="num">{micro(d.deal.fee_micro)} USDT</span></Row>
        {d.deal.close_reason && <Row label="끝난 이유" detail={d.deal.close_reason} />}
      </Group>

      <Group title="사람">
        {d.parties.map((p) => (
          <Row key={p.tg_id} label={<>{name(p.tg_id)} <span className="sub">{p.role === "creator" ? "신청자" : "상대"}</span></>}
            detail={`공지 ${p.notice_ok ? "확인" : "-"} · ${({ deposit: "예치", none: "예치 안 함" } as Record<string, string>)[p.choice || ""] || "선택 전"} · 완료 ${p.complete ? "누름" : "-"}`}>
            <span className="num">{d.balances[String(p.tg_id)] ?? "0"} USDT</span>
          </Row>
        ))}
      </Group>

      {d.deal.state === "DISPUTE" && (
        <Group title="분쟁 결정" tone="bad" footer="남은 돈을 어떻게 나눌지 적어요. 합계가 남은 금액과 같아야 해요. 수수료를 비우면 거래 수수료 그대로.">
          {d.parties.map((p) => (
            <Row key={p.tg_id} label={name(p.tg_id)}>
              <input className="field num-input" inputMode="decimal" placeholder="USDT" value={plan[p.tg_id] || ""}
                onChange={(e) => setPlan({ ...plan, [p.tg_id]: e.target.value })} />
            </Row>
          ))}
          <Row label="수수료 (선택)">
            <input className="field num-input" inputMode="decimal" placeholder="USDT" value={fee} onChange={(e) => setFee(e.target.value)} />
          </Row>
          <div className="actions pad">
            <Button tone="secondary" onClick={() => act(`${base}/resolve`, { reopen: true },
              { title: "분쟁 풀기", message: "분쟁을 풀고 이전 단계로 돌릴까요?", confirm: "돌리기" })}>분쟁 풀고 이어서</Button>
            <Button tone="destructive" onClick={() => {
              const p = Object.fromEntries(Object.entries(plan).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.trim()]));
              act(`${base}/resolve`, { plan: p, fee: fee.trim() || null },
                { title: "이 분배로 결정", message: "받을 사람에게 바로 주소를 물어봐요.", confirm: "결정", destructive: true });
            }}>이대로 결정</Button>
          </div>
        </Group>
      )}

      {d.messages.length > 0 && (
        <Group title={`대화 · 증거 (${d.messages.length})`} footer="거래방에 올라온 글과, 분쟁 중 1:1 로 받은 증거예요.">
          <div className="chat">
            {d.messages.map((m) => (
              <div key={m.id} className={`msg${m.source === "evidence" ? " evidence" : ""}${m.tg_id === d.parties[0]?.tg_id ? " left" : " right"}`}>
                <div className="msg-meta">{name(m.tg_id)} · {when(m.created_at)}{m.source === "evidence" && <Badge tone="orange">증거</Badge>}</div>
                {m.file_id && m.kind === "photo" && (
                  <a href={`/api/file/${encodeURIComponent(m.file_id)}`} target="_blank" rel="noopener noreferrer">
                    <img src={`/api/file/${encodeURIComponent(m.file_id)}`} alt="첨부 사진" loading="lazy" />
                  </a>
                )}
                {m.file_id && m.kind !== "photo" && (
                  <a className="file" href={`/api/file/${encodeURIComponent(m.file_id)}`} target="_blank" rel="noopener noreferrer">📎 {m.kind} 열기</a>
                )}
                {m.text && <div className="msg-text">{m.text}</div>}
              </div>
            ))}
          </div>
        </Group>
      )}
      {d.ratings.length > 0 && (
        <Group title="평점">
          {d.ratings.map((r) => <Row key={r.rater} label={`${name(r.rater)} 가 준 별점`}><span className="num">{"★".repeat(r.stars)}</span></Row>)}
        </Group>
      )}

      <Group title="입금">
        <Table head={["사람", "금액", "상태", "tx"]} rows={d.slots.map((s) => [name(s.tg_id), micro(s.amount_micro), s.status, s.tx_id ? <Mono>{s.tx_id}</Mono> : ""])} />
      </Group>
      <Group title="지급">
        <Table head={["받는 사람", "종류", "금액", "수수료", "주소", "상태", "tx"]}
          rows={d.payouts.map((p) => [name(p.tg_id), p.kind, micro(p.amount_micro), micro(p.fee_micro),
            p.address ? <Mono>{p.address}</Mono> : "", p.status, p.tx_id ? <Mono>{p.tx_id}</Mono> : ""])} />
      </Group>

      {open && (
        <Group title="관리자 취소" tone="bad" footer="들어온 돈이 있으면 환불 절차로 넘어가요.">
          <div className="inline pad">
            <input className="field" placeholder="취소 사유" value={reason} onChange={(e) => setReason(e.target.value)} />
            <Button tone="destructive" onClick={() => act(`${base}/cancel`, { reason },
              { title: "거래 취소", message: "이 거래를 취소할까요?", confirm: "취소하기", destructive: true })}>거래 취소</Button>
          </div>
        </Group>
      )}

      <Group title="기록">
        <Table head={["시각", "누가", "무엇", "내용"]}
          rows={d.events.map((e) => [when(e.at), name(e.actor), e.kind, <span className="json">{JSON.stringify(e.data)}</span>])} />
      </Group>
    </>
  );
}
