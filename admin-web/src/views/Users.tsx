import { useState } from "react";
import { api, ApiError, post } from "../api";
import type { Nav } from "../App";
import { Badge, Button, Group, Mono, Row, useUi } from "../ui";
import { STATE, when } from "../labels";

type U = {
  user: { tg_id: number; username: string | null; name: string; banned: boolean };
  deals: { code: string; state: string; created_at: string }[];
  addresses: string[];
};

export function Users({ nav }: { nav: Nav }) {
  const ui = useUi();
  const [q, setQ] = useState("");
  const [u, setU] = useState<U | null>(null);
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
      <form className="filters" onSubmit={(e) => { e.preventDefault(); load(); }}>
        <input className="field" type="search" placeholder="@아이디 또는 숫자 ID" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit">찾기</Button>
      </form>
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
