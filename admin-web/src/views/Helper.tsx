import { useEffect, useState } from "react";
import { api, ApiError, post } from "../api";
import { Badge, Button, Group, Row, useUi } from "../ui";

type H = { connected: boolean; user_id?: number; name?: string; username?: string; rooms_made?: number };

// 도우미 계정: 봇은 그룹을 못 만들어서, 사람 계정으로 거래방을 자동으로 만듦
export function HelperSettings() {
  const ui = useUi();
  const [h, setH] = useState<H | null>(null);
  const [f, setF] = useState({ api_id: "", api_hash: "", phone: "", code: "", password: "" });
  const [step, setStep] = useState<"form" | "code">("form");
  const load = () => api<H>("/api/helper").then(setH).catch(() => {});
  useEffect(() => { load(); }, []);

  async function run(path: string, body: unknown, next?: () => void) {
    try { const r = await post(path, body); ui.toast(r.result); next?.(); load(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value });

  if (!h) return null;
  if (h.connected) {
    return (
      <Group title="도우미 계정 (거래방 자동 생성)" footer="빈 거래방이 없으면 이 계정이 새 방을 만들고 봇을 관리자로 넣어요. 다 쓴 방은 비워서 다시 써요.">
        <Row label={<>{h.name} {h.username && <span className="sub">@{h.username}</span>}</>} detail={`숫자 ID ${h.user_id} · 만든 방 ${h.rooms_made ?? 0}개`}>
          <Badge tone="green">연결됨</Badge>
        </Row>
        <div className="actions pad">
          <Button tone="destructive" onClick={async () => {
            if (await ui.ask({ title: "연결 끊기", message: "끊으면 빈 방이 없을 때 1:1 로 진행돼요.", confirm: "끊기", destructive: true }))
              run("/api/helper/logout", {});
          }}>연결 끊기</Button>
        </div>
      </Group>
    );
  }
  return (
    <Group title="도우미 계정 (거래방 자동 생성)"
      footer="my.telegram.org → API development tools 에서 api_id·api_hash 를 받아 넣으세요. 이 계정이 거래방을 만드는 관리자가 돼요.">
      {step === "form" ? (
        <>
          <Row label="api_id"><input className="field num-input" inputMode="numeric" value={f.api_id} onChange={set("api_id")} /></Row>
          <Row label="api_hash"><input className="field" autoComplete="off" value={f.api_hash} onChange={set("api_hash")} /></Row>
          <Row label="전화번호"><input className="field" inputMode="tel" placeholder="+82 10…" value={f.phone} onChange={set("phone")} /></Row>
          <div className="actions pad">
            <Button onClick={() => run("/api/helper/start", { api_id: Number(f.api_id), api_hash: f.api_hash, phone: f.phone },
              () => setStep("code"))}>코드 받기</Button>
          </div>
        </>
      ) : (
        <>
          <Row label="로그인 코드" detail="텔레그램 앱 '텔레그램' 공식 대화방으로 온 숫자">
            <input className="field num-input" inputMode="numeric" value={f.code} onChange={set("code")} />
          </Row>
          <Row label="2단계 비밀번호" detail="설정해 둔 경우만">
            <input className="field" type="password" autoComplete="off" value={f.password} onChange={set("password")} />
          </Row>
          <div className="actions pad">
            <Button tone="plain" onClick={() => setStep("form")}>처음부터</Button>
            <Button onClick={() => run("/api/helper/code", { code: f.code, password: f.password })}>연결</Button>
          </div>
        </>
      )}
    </Group>
  );
}
