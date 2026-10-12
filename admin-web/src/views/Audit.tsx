import { useEffect, useState } from "react";
import { api } from "../api";
import { Group, Table } from "../ui";
import { when } from "../labels";

type E = { at: string; deal_id: number | null; actor: number | null; kind: string; data: unknown };

export function Audit() {
  const [rows, setRows] = useState<E[] | null>(null);
  useEffect(() => { api<E[]>("/api/audit").then(setRows).catch(() => {}); }, []);
  if (!rows) return null;
  return (
    <Group>
      <Table head={["시각", "거래", "누가", "무엇", "내용"]}
        rows={rows.map((e) => [when(e.at), e.deal_id ?? "", e.actor ?? "시스템", e.kind, <span className="json">{JSON.stringify(e.data)}</span>])} />
    </Group>
  );
}
