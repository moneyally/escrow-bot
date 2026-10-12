import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Empty, Group, Row, useUi } from "../ui";

type R = { chat_id: number; title: string | null; state: string; code: string | null; note: string | null };
const LABEL: Record<string, [string, "green" | "blue" | "red"]> = { free: ["비어 있음", "green"], used: ["사용 중", "blue"], broken: ["고장", "red"] };

export function Rooms({ nav, tick }: { nav: Nav; tick: number }) {
  const ui = useUi();
  const [rows, setRows] = useState<R[] | null>(null);
  useEffect(() => { api<R[]>("/api/rooms").then(setRows).catch(() => {}); }, [tick]);
  async function reset(r: R) {
    if (!(await ui.ask({ title: "방 다시 쓰기", message: "방에 아무도 없는지 확인했나요?", confirm: "다시 쓰기" }))) return;
    try { const x = await post(`/api/rooms/${r.chat_id}/reset`); ui.toast(x.result); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }
  if (!rows) return null;
  return (
    <>
      <p className="hint">비공개 슈퍼그룹을 만들고 '새 멤버에게 대화 기록 숨기기'를 켠 뒤, 봇을 관리자(사용자 초대·차단·메시지 삭제)로 넣으면 자동 등록돼요. 넣는 사람은 관리자 계정이어야 해요.</p>
      {!rows.length ? <Empty>등록된 거래방이 없어요.</Empty> : (
        <Group>
          {rows.map((r) => {
            const [label, tone] = LABEL[r.state] || [r.state, "blue"];
            return (
              <Row key={r.chat_id} label={r.title || String(r.chat_id)} detail={[r.code, r.note].filter(Boolean).join(" · ") || undefined}>
                <Badge tone={tone}>{label}</Badge>
                {r.state === "broken" && <Button tone="secondary" small onClick={() => reset(r)}>다시 쓰기</Button>}
              </Row>
            );
          })}
        </Group>
      )}
    </>
  );
}
