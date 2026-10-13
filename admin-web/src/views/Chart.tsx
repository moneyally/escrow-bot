import { useEffect, useState } from "react";
import { api } from "../api";
import { Group } from "../ui";
import { micro } from "../labels";

type Day = { day: string; new_deals: number; done: number; volume_micro: number; fees_micro: number };

// 최근 30일 막대 그래프 (수수료 / 예치 금액 / 거래 수 전환). 값은 막대 위에 마우스·터치하면 보임.
export function RevenueChart({ tick }: { tick: number }) {
  const [rows, setRows] = useState<Day[] | null>(null);
  const [m, setM] = useState<"fees" | "volume" | "deals">("fees");
  const [hover, setHover] = useState<Day | null>(null);
  useEffect(() => { api<Day[]>("/api/revenue?days=30").then(setRows).catch(() => {}); }, [tick]);
  if (!rows) return null;
  const val = (d: Day) => (m === "fees" ? d.fees_micro / 1e6 : m === "volume" ? d.volume_micro / 1e6 : d.new_deals);
  const max = Math.max(1e-9, ...rows.map(val));
  const total = rows.reduce((a, d) => a + val(d), 0);
  const unit = m === "deals" ? "건" : "USDT";
  const fmt = (v: number) => (m === "deals" ? String(v) : micro(Math.round(v * 1e6)));
  const shown = hover ?? rows[rows.length - 1];
  return (
    <Group title="최근 30일">
      <div className="chart">
        <div className="chart-head">
          <div className="segmented" role="tablist" aria-label="보기">
            {([["fees", "수수료"], ["volume", "예치 금액"], ["deals", "새 거래"]] as const).map(([k, l]) => (
              <button key={k} role="tab" aria-selected={m === k} className={m === k ? "on" : ""} onClick={() => setM(k)}>{l}</button>
            ))}
          </div>
          <div className="chart-total"><b className="num">{fmt(total)}</b> <span className="sub">{unit} · 30일 합계</span></div>
        </div>
        <div className="bars" onMouseLeave={() => setHover(null)}>
          {rows.map((d) => (
            <div key={d.day} className={`bar${hover?.day === d.day ? " on" : ""}`} onMouseEnter={() => setHover(d)} onTouchStart={() => setHover(d)}
              title={`${d.day} · ${fmt(val(d))} ${unit}`}>
              <span style={{ height: `${Math.max(val(d) > 0 ? 3 : 0, (val(d) / max) * 100)}%` }} />
            </div>
          ))}
        </div>
        <div className="chart-foot sub">
          <span>{rows[0].day.slice(5)}</span>
          <span>{shown.day.slice(5)} · <b className="num">{fmt(val(shown))}</b> {unit} · 끝난 거래 {shown.done}건</span>
          <span>{rows[rows.length - 1].day.slice(5)}</span>
        </div>
      </div>
    </Group>
  );
}
