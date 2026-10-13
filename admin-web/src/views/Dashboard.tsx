import { useEffect, useState } from "react";
import { api, type Stats } from "../api";
import type { Nav } from "../App";
import { Badge, Group, Mono, Row } from "../ui";
import { micro } from "../labels";

type Me = { tg_id: number; bot_name: string; pay_address: string; stub: string[] };

export function Dashboard({ stats: s, nav, tick }: { stats: Stats | null; nav: Nav; tick: number }) {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => { api<Me>("/api/me").then(setMe).catch(() => {}); }, [tick]);
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
      <Group title="지갑" footer="장부 = 예치금 − 보낸 돈 (번 수수료 포함). 지갑 잔액은 한 시간마다 조회해요.">
        <Row label="장부상 있어야 할 돈"><span className="num">{s.ledger} USDT</span></Row>
        <Row label="실제 지갑 잔액">
          <span className="num">{s.wallet != null ? `${s.wallet} USDT` : "아직 조회 전"}</span>
          {diff != null && (diff === 0 ? <Badge tone="green">일치</Badge> : <Badge tone={diff < 0 ? "red" : "orange"}>차이 {micro(diff)}</Badge>)}
        </Row>
        <Row label="번 수수료"><span className="num">{s.fees} USDT</span></Row>
        {me && <Row label="받는 주소" detail={<Mono>{me.pay_address}</Mono>} />}
      </Group>
    </>
  );
}
