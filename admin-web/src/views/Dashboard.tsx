import { useEffect, useState } from "react";
import { api, type Stats } from "../api";
import type { Nav } from "../App";
import { Badge, Group, Mono, Row } from "../ui";
import { KIND, STATE, micro, when } from "../labels";

type Me = { tg_id: number; bot_name: string; pay_address: string; stub: string[] };
type Todo = { stuck: { code: string; state: string; since: string }[]; watch_age: number | null;
  hints: { mode: string; recent: number; approved: number }[] };

export function Dashboard({ stats: s, nav, tick }: { stats: Stats | null; nav: Nav; tick: number }) {
  const [me, setMe] = useState<Me | null>(null);
  const [todo, setTodo] = useState<Todo | null>(null);
  useEffect(() => {
    api<Me>("/api/me").then(setMe).catch(() => {});
    api<Todo>("/api/todo").then(setTodo).catch(() => {});
  }, [tick]);
  if (!s) return null;
  const diff = s.wallet_micro != null ? s.wallet_micro - s.ledger_micro : null;
  const tiles: [string, number | string, string | null][] = [
    ["확인 대기", s.pending_approvals, "approvals"], ["보낼 것", s.payouts_to_send, "payouts"],
    ["확인 필요 입금", s.unmatched_in, "deposits"], ["분쟁", s.disputes, "disputes"],
    ["진행 중 거래", s.active, "deals"], ["오늘 새 거래", s.today, null],
    ["끝난 거래", s.done_total, null], ["빈 거래방", s.rooms_free, "rooms"],
  ];
  return (
    <>
      {me && me.stub.length > 0 && (
        <Group tone="warn"><Row label="아직 설정 안 된 값" detail={`${me.stub.join(", ")} — 서버 .env 를 채워야 실제로 운영돼요.`} /></Group>
      )}
      <div className="tiles">
        {tiles.map(([label, v, to]) => {
          const urgent = to && ["approvals", "payouts", "deposits", "disputes"].includes(to) && Number(v) > 0;
          const body = <><b className={urgent ? "urgent" : ""}>{v}</b><span>{label}</span></>;
          return to
            ? <button key={label} className="tile" onClick={() => nav.go(to)}>{body}</button>
            : <div key={label} className="tile">{body}</div>;
        })}
      </div>
      {todo && (todo.watch_age == null || todo.watch_age > 300) && me && !me.stub.includes("PAY_ADDRESS") && (
        <Group tone="bad"><Row label="입금 감시가 멈춰 있어요"
          detail={todo.watch_age == null ? "아직 한 번도 안 돌았어요." : `마지막으로 돈 지 ${Math.round(todo.watch_age / 60)}분`} /></Group>
      )}
      {todo && todo.stuck.length > 0 && (
        <Group title="오래 멈춘 거래" footer="6시간 넘게 같은 단계에 있는 거래예요. 눌러서 무엇을 기다리는지 보세요.">
          {todo.stuck.map((d) => (
            <Row key={d.code} chevron onClick={() => nav.openDeal(d.code)} label={<Mono>{d.code}</Mono>}
              detail={`${STATE[d.state] || d.state} · ${when(d.since)}부터`} />
          ))}
        </Group>
      )}
      <Group title="지갑" footer="장부 = 예치금 − 보낸 돈 (번 수수료 포함). 지갑 잔액은 한 시간마다 조회해요.">
        <Row label="장부상 있어야 할 돈"><span className="num">{s.ledger} USDT</span></Row>
        <Row label="실제 지갑 잔액">
          <span className="num">{s.wallet != null ? `${s.wallet} USDT` : "아직 조회 전"}</span>
          {diff != null && (diff === 0 ? <Badge tone="green">일치</Badge> : <Badge tone={diff < 0 ? "red" : "orange"}>차이 {micro(diff)}</Badge>)}
        </Row>
        <Row label="번 수수료"><span className="num">{s.fees} USDT</span></Row>
        {me && <Row label="받는 주소" detail={<Mono>{me.pay_address}</Mono>} />}
      </Group>
      {todo && (
        <Group title="단계별 자동화 참고" footer="최근 30건 중 승인된 비율이에요. 오래 문제없이 전부 승인됐다면 설정에서 그 단계를 자동으로 바꿔도 돼요.">
          {todo.hints.map((h) => (
            <Row key={h.mode} label={KIND[h.mode] || h.mode}>
              <span className="num">{h.recent ? `${h.approved}/${h.recent} 승인` : "기록 없음"}</span>
              {h.recent >= 20 && h.approved === h.recent && <Badge tone="green">자동 고려</Badge>}
            </Row>
          ))}
        </Group>
      )}
    </>
  );
}
