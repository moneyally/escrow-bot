import { useEffect, useState } from "react";
import { api, ApiError, put } from "../api";
import type { Nav } from "../App";
import { Button, Group, Row, useUi } from "../ui";
import { SETTING_LABEL } from "../labels";
import { HelperSettings } from "./Helper";

type V = Record<string, string | number | boolean>;

export function Settings({ nav }: { nav: Nav }) {
  const ui = useUi();
  const [v, setV] = useState<V | null>(null);
  const [text, setText] = useState<Record<string, string>>({});   // 숫자 칸은 글자로 들고 있다가 저장할 때 바꿈
  useEffect(() => {
    api<{ values: V }>("/api/settings").then(({ values }) => {
      setV(values);
      setText(Object.fromEntries(Object.entries(values).filter(([, x]) => typeof x === "number")
        .map(([k, x]) => [k, k.endsWith("_micro") ? String(Number(x) / 1e6) : String(x)])));
    }).catch(() => {});
  }, []);
  if (!v) return null;
  const modes = Object.keys(v).filter((k) => k.startsWith("mode."));
  const nums = Object.keys(text);

  async function save() {
    const body: V = { ...v! };
    for (const k of nums) {
      const n = Number(text[k] || "0");
      if (!Number.isFinite(n) || n < 0) return ui.toast(`${SETTING_LABEL[k] || k} 값이 이상해요`);
      body[k] = k.endsWith("_micro") ? Math.round(n * 1e6) : Math.round(n);
    }
    if (!(await ui.ask({ title: "설정 저장", message: "바로 적용돼요.", confirm: "저장" }))) return;
    try { const r = await put("/api/settings", body); ui.toast(r.result); nav.refresh(); }
    catch (e) { if (e instanceof ApiError) ui.toast(e.message); }
  }

  return (
    <>
      <Group title="단계별 처리" footer="수동 = 관리자가 승인해야 다음 단계 · 자동 = 조건이 맞으면 바로 (자동 상한을 넘는 금액은 수동)">
        {modes.map((k) => (
          <Row key={k} label={SETTING_LABEL[k] || k}>
            <div className="segmented" role="radiogroup" aria-label={SETTING_LABEL[k] || k}>
              {(["manual", "auto"] as const).map((m) => (
                <button key={m} role="radio" aria-checked={v[k] === m} className={v[k] === m ? "on" : ""}
                  onClick={() => setV({ ...v, [k]: m })}>{m === "manual" ? "수동" : "자동"}</button>
              ))}
            </div>
          </Row>
        ))}
      </Group>
      <Group title="금액 · 시간" footer="금액은 USDT 단위">
        {nums.map((k) => (
          <Row key={k} label={SETTING_LABEL[k] || k}>
            <input className="field num-input" inputMode="decimal" value={text[k]} onChange={(e) => setText({ ...text, [k]: e.target.value })} />
          </Row>
        ))}
      </Group>
      <Group>
        <Row label="새 거래 받기">
          <label className="switch">
            <input type="checkbox" checked={!!v.new_deals} onChange={(e) => setV({ ...v, new_deals: e.target.checked })} />
            <span aria-hidden />
          </label>
        </Row>
      </Group>
      <div className="actions"><Button onClick={save}>저장</Button></div>
      <div style={{ height: 24 }} />
      <HelperSettings />
    </>
  );
}
