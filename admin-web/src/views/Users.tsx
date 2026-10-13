import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Group, Mono, Row, useUi } from "../ui";
import { STATE, when } from "../labels";

type U = {
  user: { tg_id: number; username: string | null; name: string; banned: boolean };
  deals: { code: string; state: string; created_at: string }[];
  addresses: string[];
};

type L = { tg_id: number; username: string | null; name: string; banned: boolean; done: number; active: number; disputes: number;
  volume: string; rating: number | null; ratings: number; updated_at: string };

export function Users({ nav }: { nav: Nav }) {
  const ui = useUi();
  const [q, setQ] = useState("");
  const [u, setU] = useState<U | null>(null);
  const [list, setList] = useState<L[] | null>(null);
  const [sort, setSort] = useState("recent");
  const loadList = (query = q) =>
    api<L[]>(`/api/users?sort=${sort}&q=${encodeURIComponent(query.trim())}`).then(setList).catch(() => {});
  useEffect(() => { loadList(); }, [sort]);   // eslint-disable-line react-hooks/exhaustive-deps
  async function load(key = q.trim()) {
    if (!key) return;
    try { setU(await api<U>("/api/users/" + encodeURIComponent(key))); }
    catch (e) { setU(null); if (e instanceof ApiError) ui.toast(e.message); }
  }
  async function ban(b: boolean) {
    if (!u) return;
    if (!(await ui.ask({ title: b ? "이용 제한" : "제한 풀기", message: `@${u.user.username || u.user.tg_id}`, confirm: b ? "제한" : "풀기", destructive: b }))) return;
    try { const r = await post(`/api/users/${u.user.tg_id}/ban`, { banned: b }); ui.toast(r.result); load(String(u.user.tg_id)); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }
  return (
    <>
      <form className="filters" onSubmit={(e) => { e.preventDefault(); setU(null); loadList(); }}>
        <select className="field" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="정렬">
          <option value="recent">최근 활동순</option><option value="deals">끝낸 거래순</option>
          <option value="volume">예치 금액순</option><option value="disputes">분쟁 많은 순</option>
        </select>
        <input className="field" type="search" placeholder="@아이디 · 이름 · 숫자 ID" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit">찾기</Button>
      </form>
      {!u && list && (
        <Group title={`사용자 ${list.length}명`}>
          {list.map((x) => (
            <Row key={x.tg_id} chevron onClick={() => load(String(x.tg_id))}
              label={<>{x.username ? "@" + x.username : x.name || x.tg_id} {x.banned && <Badge tone="red">제한</Badge>}
                {x.disputes > 0 && <Badge tone="orange">분쟁 {x.disputes}</Badge>}</>}
              detail={`끝낸 거래 ${x.done} · 진행 중 ${x.active} · 예치 ${x.volume} USDT · ${when(x.updated_at)}`}>
              {x.rating != null && <span className="num">⭐{x.rating} ({x.ratings})</span>}
            </Row>
          ))}
        </Group>
      )}
      {u && <div className="actions left" style={{ marginBottom: 12 }}><Button tone="plain" onClick={() => setU(null)}>‹ 목록</Button></div>}
      {u && (
        <>
          <Group>
            <Row label={<>@{u.user.username || "-"} {u.user.banned && <Badge tone="red">제한됨</Badge>}</>} detail={u.user.name}>
              <Mono>{u.user.tg_id}</Mono>
            </Row>
            <div className="actions pad">
              <Button tone={u.user.banned ? "secondary" : "destructive"} onClick={() => ban(!u.user.banned)}>
                {u.user.banned ? "제한 풀기" : "이용 제한"}
              </Button>
            </div>
          </Group>
          <Group title="쓴 주소">
            {u.addresses.length ? u.addresses.map((a) => <Row key={a} detail={<Mono>{a}</Mono>} />) : <Row detail="없음" />}
          </Group>
          <Group title="거래">
            {u.deals.length ? u.deals.map((d) => (
              <Row key={d.code} chevron onClick={() => nav.openDeal(d.code)} label={<Mono>{d.code}</Mono>} detail={when(d.created_at)}>
                <Badge>{STATE[d.state] || d.state}</Badge>
              </Row>
            )) : <Row detail="없음" />}
          </Group>
        </>
      )}
    </>
  );
}
