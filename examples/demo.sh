#!/usr/bin/env bash
# 端到端演示：登记 -> 加工 -> 残留继承 -> 清洗 -> 撤销 -> 声明更正 -> 标签签发
# 用法：BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { echo -e "\n\033[1;36m== $* ==\033[0m"; }
req() { # METHOD PATH JSON
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo
}

step "1. 登记原料批次（含过敏原 / 待确认）"
req POST /materials '{"batch_id":"RM-MILK","allergens":["milk"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-OAT","allergens":["gluten"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-MYST","pending":true,"declared_at":"2026-01-02T06:00:00Z"}'

step "2. 登记加工批次（EQ-1 共线：先奶制品，后燕麦品）"
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-1","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-MILK"],"outputs":["OUT-MILK-1"]}'
req POST /processings '{"processing_id":"P2","equipment_id":"EQ-1","start_utc":"2026-01-02T10:00:00Z","end_utc":"2026-01-02T11:00:00Z","inputs":["RM-OAT","RM-MYST"],"outputs":["OUT-OAT-1"]}'

step "3. 溯源 OUT-OAT-1：gluten 来自投入，milk 来自设备残留，pending 来自 RM-MYST"
req GET /batches/OUT-OAT-1/trace

step "4. 登记两次加工之间的已验证清洗 -> 残留被清除"
req POST /cleanings '{"equipment_id":"EQ-1","cleaned_at":"2026-01-02T09:30:00Z","verified":true}'
req GET /batches/OUT-OAT-1/trace

step "5. 撤销清洗 -> 未签发结论实时重算，milk 残留恢复"
req POST /cleanings/1/revoke
req GET /batches/OUT-OAT-1/trace

step "6. 原料声明更正：RM-OAT 实际含 sesame"
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame"]}'
req GET /batches/OUT-OAT-1/trace

step "7. 签发标签（冻结当前结论）；再更正声明后签发关联旧版的新标签"
req POST /batches/OUT-OAT-1/labels '{}'
req POST /materials/RM-OAT/declarations '{"allergens":["gluten","sesame","soy"]}'
req POST /batches/OUT-OAT-1/labels '{"supersedes_id":1}'
step "旧标签保持不变"
req GET /labels/1
step "新标签反映最新结论"
req GET /labels/2

step "8. 预演：先登记一条新的已验证清洗，再预演其撤销（不落库）"
req POST /materials '{"batch_id":"RM-RICE","allergens":[],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /processings '{"processing_id":"P10","equipment_id":"EQ-2","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-MILK"],"outputs":["OUT-R-1"]}'
req POST /processings '{"processing_id":"P11","equipment_id":"EQ-2","start_utc":"2026-01-02T10:00:00Z","end_utc":"2026-01-02T11:00:00Z","inputs":["RM-RICE"],"outputs":["OUT-R-2"]}'
CLEAN_ID="$(curl -sS "$BASE/cleanings" -X POST -H 'Content-Type: application/json' \
  -d '{"equipment_id":"EQ-2","cleaned_at":"2026-01-02T09:30:00Z","verified":true}' \
  | sed -n 's/.*"id": *\([0-9]*\).*/\1/p')"
echo "registered cleaning id=$CLEAN_ID"
# 清洗撤销预演：撤销后 milk 经设备残留进入 OUT-R-2
curl -sS "$BASE/previews/cleanings/$CLEAN_ID/revocation" -X POST | tee /tmp/pv-cleaning.json; echo
CLEAN_TOKEN="$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' /tmp/pv-cleaning.json)"
step "凭据提交撤销（原子应用；若受影响记录已变化会 409 要求重新预演）"
req POST /previews/commit "{\"credential\":\"$CLEAN_TOKEN\"}"

step "9. 预演：原料声明更正（OUT-R-2 将新增 sesame；已签发标签差异一并列出）"
req POST /batches/OUT-R-2/labels '{}'
curl -sS "$BASE/previews/material-declarations" -X POST \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"RM-RICE","allergens":["sesame"]}' | tee /tmp/pv-decl.json; echo
DECL_TOKEN="$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' /tmp/pv-decl.json)"
req POST /previews/commit "{\"credential\":\"$DECL_TOKEN\"}"
step "旧标签保持签发时结论，实时结论已含 sesame"
req GET /batches/OUT-R-2/trace

step "10. 非法登记示例（均被拒绝且不影响已有数据）"req POST /processings '{"processing_id":"P-BAD","equipment_id":"EQ-1","start_utc":"2026-01-02T08:30:00Z","end_utc":"2026-01-02T09:30:00Z","inputs":["RM-OAT"],"outputs":["OUT-X"]}'
req POST /processings '{"processing_id":"P-LOOP","equipment_id":"EQ-9","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-OAT"],"outputs":["RM-OAT"]}'
req POST /processings '{"processing_id":"P-TIME","equipment_id":"EQ-9","start_utc":"2026-01-02T09:00:00Z","end_utc":"2026-01-02T08:00:00Z","inputs":["RM-OAT"],"outputs":["OUT-Y"]}'
