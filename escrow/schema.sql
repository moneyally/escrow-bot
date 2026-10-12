-- 에스크로 DB (PostgreSQL 16). 금액은 전부 BIGINT 마이크로 USDT. 시작할 때 db.migrate 가 이 파일을 그대로 실행 (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS users (
    tg_id       BIGINT PRIMARY KEY,
    username    TEXT,
    name        TEXT NOT NULL DEFAULT '',
    banned      BOOLEAN NOT NULL DEFAULT FALSE,
    tutorial_done BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_username ON users (lower(username));

CREATE TABLE IF NOT EXISTS rooms (
    chat_id     BIGINT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'free' CHECK (state IN ('free', 'used', 'broken')),
    deal_id     BIGINT,
    invite_link TEXT,
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deals (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    state       TEXT NOT NULL,
    prev_state  TEXT,                       -- 분쟁 들어가기 전 상태
    creator     BIGINT NOT NULL REFERENCES users (tg_id),
    partner     BIGINT REFERENCES users (tg_id),
    partner_hint TEXT,                      -- 신청 때 입력한 @아이디 (상대가 아직 봇을 안 켰을 때)
    room_id     BIGINT REFERENCES rooms (chat_id),
    fee_micro   BIGINT NOT NULL CHECK (fee_micro >= 0),
    deadline    TIMESTAMPTZ,                -- 이 단계 시간 초과 시각
    close_reason TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS deals_state ON deals (state);
CREATE INDEX IF NOT EXISTS deals_deadline ON deals (deadline) WHERE deadline IS NOT NULL;

CREATE TABLE IF NOT EXISTS deal_parties (
    deal_id     BIGINT NOT NULL REFERENCES deals (id),
    tg_id       BIGINT NOT NULL REFERENCES users (tg_id),
    role        TEXT NOT NULL CHECK (role IN ('creator', 'partner')),
    joined      BOOLEAN NOT NULL DEFAULT FALSE,   -- 거래방 들어옴
    notice_ok   BOOLEAN NOT NULL DEFAULT FALSE,
    choice      TEXT CHECK (choice IN ('deposit', 'none')),
    complete    BOOLEAN NOT NULL DEFAULT FALSE,   -- 거래완료 누름
    cancel_req  BOOLEAN NOT NULL DEFAULT FALSE,   -- 입금 뒤 취소 동의
    PRIMARY KEY (deal_id, tg_id)
);

-- 입금 자리: 거래·사람마다 '끝자리까지 정확한 금액'. 기다리는 동안 같은 금액은 다른 자리에 못 줌.
CREATE TABLE IF NOT EXISTS deposit_slots (
    id          BIGSERIAL PRIMARY KEY,
    deal_id     BIGINT NOT NULL REFERENCES deals (id),
    tg_id       BIGINT NOT NULL REFERENCES users (tg_id),
    base_micro  BIGINT NOT NULL CHECK (base_micro > 0),
    amount_micro BIGINT NOT NULL CHECK (amount_micro >= base_micro),
    status      TEXT NOT NULL CHECK (status IN ('waiting', 'received', 'confirmed', 'expired', 'cancelled')),
    tx_id       TEXT,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS slot_amount_waiting ON deposit_slots (amount_micro) WHERE status = 'waiting';
CREATE UNIQUE INDEX IF NOT EXISTS slot_one_open ON deposit_slots (deal_id, tg_id) WHERE status IN ('waiting', 'received', 'confirmed');

-- 체인에서 확인한 USDT 이동 (우리 주소 기준). tx 하나에 Transfer 가 여럿일 수 있어 (tx_id, idx).
CREATE TABLE IF NOT EXISTS chain_tx (
    tx_id       TEXT NOT NULL,
    idx         INT NOT NULL DEFAULT 0,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    from_addr   TEXT NOT NULL,
    to_addr     TEXT NOT NULL,
    amount_micro BIGINT NOT NULL,
    block_ts    TIMESTAMPTZ NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('matched', 'unmatched', 'returned', 'ignored', 'flagged')),
    slot_id     BIGINT REFERENCES deposit_slots (id),
    payout_id   BIGINT,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tx_id, idx)
);
CREATE INDEX IF NOT EXISTS chain_tx_status ON chain_tx (status);

-- 분배안: plan = {"<tg_id>": 마이크로, ...} (수수료 빼기 전 금액)
CREATE TABLE IF NOT EXISTS splits (
    id          BIGSERIAL PRIMARY KEY,
    deal_id     BIGINT NOT NULL REFERENCES deals (id),
    kind        TEXT NOT NULL CHECK (kind IN ('final', 'refund', 'dispute', 'partial')),
    plan        JSONB NOT NULL,
    proposer    BIGINT,                      -- NULL = 관리자 결정
    status      TEXT NOT NULL CHECK (status IN ('proposed', 'agreed', 'rejected', 'void')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payouts (
    id          BIGSERIAL PRIMARY KEY,
    deal_id     BIGINT NOT NULL REFERENCES deals (id),
    split_id    BIGINT REFERENCES splits (id),
    tg_id       BIGINT NOT NULL REFERENCES users (tg_id),       -- 받는 사람
    payer       BIGINT REFERENCES users (tg_id),                -- 부분 지급: 누구 몫에서 나가는지 (정산·환불은 NULL)
    kind        TEXT NOT NULL CHECK (kind IN ('partial', 'final', 'refund')),
    gross_micro BIGINT NOT NULL CHECK (gross_micro > 0),
    fee_micro   BIGINT NOT NULL DEFAULT 0 CHECK (fee_micro >= 0),
    amount_micro BIGINT NOT NULL CHECK (amount_micro > 0),
    address     TEXT,
    status      TEXT NOT NULL CHECK (status IN ('need_address', 'review', 'approved', 'sent', 'confirmed', 'cancelled')),
    tx_id       TEXT UNIQUE,
    approved_by BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (amount_micro = gross_micro - fee_micro)
);
CREATE INDEX IF NOT EXISTS payouts_status ON payouts (status);

-- 관리자 확인 대기 (단계별 수동 모드). 같은 대상에 열린 건 하나만.
CREATE TABLE IF NOT EXISTS approvals (
    id          BIGSERIAL PRIMARY KEY,
    deal_id     BIGINT REFERENCES deals (id),
    kind        TEXT NOT NULL,               -- deposit · start · partial · final · refund · payout_done
    ref_id      BIGINT NOT NULL,             -- 입금 자리 / 거래 / 분배안 / 지급 id
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'held', 'void')),
    note        TEXT NOT NULL DEFAULT '',
    decided_by  BIGINT,
    decided_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS approvals_open ON approvals (kind, ref_id) WHERE status IN ('pending', 'held');

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_by  BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 사람마다 지금 기다리는 1:1 글자 입력 하나 (상대 아이디·예치 금액·받을 주소 …)
CREATE TABLE IF NOT EXISTS inputs (
    tg_id       BIGINT PRIMARY KEY,
    kind        TEXT NOT NULL,
    deal_id     BIGINT,
    ref_id      BIGINT,
    data        JSONB NOT NULL DEFAULT '{}',
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS addresses (
    tg_id       BIGINT NOT NULL,
    address     TEXT NOT NULL,
    last_used   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, address)
);

-- 보낼 텔레그램 일 (메시지·방 열기·방 정리). 상태 변경과 같은 트랜잭션에서 넣고 봇이 보냄 → 재시작에도 안 잃음.
CREATE TABLE IF NOT EXISTS outbox (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'msg',  -- msg · room_open · room_close
    chat_id     BIGINT,
    text        TEXT NOT NULL DEFAULT '',
    buttons     JSONB,
    payload     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    attempts    INT NOT NULL DEFAULT 0,
    error       TEXT,
    deal_id     BIGINT,
    message_id  BIGINT                       -- 보낸 메시지 id (거래방 정리 때 지움)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (id) WHERE sent_at IS NULL;

CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL
);

-- 감사 기록: 고치기·지우기 금지
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    deal_id     BIGINT,
    actor       BIGINT,                      -- 텔레그램 ID, 0 = 시스템
    kind        TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}',
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_deal ON events (deal_id, id);

CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events 는 고치거나 지울 수 없음';
END $$;
DROP TRIGGER IF EXISTS events_no_change ON events;
CREATE TRIGGER events_no_change BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_append_only();

-- 관리 페이지 로그인: 봇 /login 이 주는 1회용 링크 (토큰은 해시만 저장, 5분)
CREATE TABLE IF NOT EXISTS admin_logins (
    token_hash  TEXT PRIMARY KEY,
    tg_id       BIGINT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
