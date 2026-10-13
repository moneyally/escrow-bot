export const KIND: Record<string, string> = {
  deposit: "입금 확인", start: "거래 시작", partial: "부분 지급", final: "최종 정산", refund: "환불", payout_done: "송금 완료 확인",
};
export const STATE: Record<string, string> = {
  INVITED: "상대 수락 대기", ROOM: "거래방 입장", NOTICE: "공지 확인", CHOOSE: "예치 선택", DEPOSIT: "입금 대기",
  START_REVIEW: "시작 승인 대기", FUNDED: "거래 중", SPLIT: "정산 방법", ADDRESS: "받을 주소", PAYOUT: "송금 대기",
  DISPUTE: "분쟁", REFUND: "환불 대기", DONE: "끝", CANCELLED: "취소",
};
export const PAYOUT_KIND: Record<string, string> = { partial: "부분 지급", final: "정산", refund: "환불" };
export const SETTING_LABEL: Record<string, string> = {
  "mode.deposit": "입금 인정", "mode.start": "거래 시작", "mode.partial": "부분 지급", "mode.final": "최종 정산",
  "mode.refund": "환불", "mode.payout_done": "송금 완료 확인", fee_micro: "수수료 (비율 방식이면 최소)", fee_percent_bp: "수수료 비율 (150 = 1.5%, 0 = 고정)", fee_max_micro: "수수료 최대 (0 = 없음)", max_held_micro: "동시 보관 한도 (0 = 없음)", min_micro: "최소 예치",
  refund_fee_micro: "환불 수수료", auto_max_micro: "자동 상한 (0 = 없음)", stage_minutes: "단계 시간 (분)",
  slot_minutes: "입금 안내 시간 (분)", slot_grace_minutes: "늦은 입금 허용 (분)", new_deals: "새 거래 받기",
};
export const micro = (v: number) => (v / 1e6).toLocaleString("ko-KR", { maximumFractionDigits: 6 });
export const when = (s: string) => new Date(s).toLocaleString("ko-KR", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
