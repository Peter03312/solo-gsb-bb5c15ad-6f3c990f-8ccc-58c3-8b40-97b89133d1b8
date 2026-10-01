#!/usr/bin/env bash
# 可取消通行凭证调用示例（签发 / 查询 / 取消 / 凭证约束区域变更）。
#
# 演示：
#   1. 登记一个 11 月生效的方区 A（[0,100]×[0,100]）
#   2. 为从区域上方安全经过的未来轨迹签发凭证 → 201，返回凭证 ID/region_revision/轨迹
#   3. 穿越轨迹、首点已过的轨迹分别被 422 拒绝（unsafe_trajectory /
#      trajectory_already_started），不落库
#   4. 按 ID 查询凭证状态；未知 ID 返回 404
#   5. 取消凭证；重复取消结果不变（幂等）
#   6. 凭证生效期间，把区域扩大到轨迹上 → 409 pass_conflict，整笔回滚；
#      取消凭证后再扩大 → 成功
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_passes.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

echo "== 1. 登记 11 月生效方区 A =="
A_ID=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "A区-凭证演示",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-11-01T00:00:00Z",
        "valid_to":   "2026-12-01T00:00:00Z"
      }' | J "['region']['id']")
echo "A 区 id=$A_ID"

echo "== 2. 签发凭证：未来轨迹从区域上方（y=200）安全经过 =="
PASS=$(curl -fsS -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x": -50, "y": 200, "t": "2026-11-15T00:00:00Z"},
          {"x": 150, "y": 200, "t": "2026-11-15T00:01:00Z"}
        ]
      }')
echo "$PASS" | python3 -m json.tool
PASS_ID=$(echo "$PASS" | J "['id']")
echo "凭证 id=$PASS_ID，签发时 region_revision=$(echo "$PASS" | J "['region_revision']")"

echo "== 3a. 穿越轨迹签发被拒：422 unsafe_trajectory（附穿越明细，不落库） =="
curl -sS -o /tmp/pass_unsafe.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x": -50, "y": 50, "t": "2026-11-15T00:00:00Z"},
          {"x": 150, "y": 50, "t": "2026-11-15T00:01:00Z"}
        ]
      }'
python3 -m json.tool /tmp/pass_unsafe.json

echo "== 3b. 首点已过的轨迹被拒：422 trajectory_already_started（不落库） =="
curl -sS -o /tmp/pass_started.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x": -50, "y": 200, "t": "2020-01-01T00:00:00Z"},
          {"x": 150, "y": 200, "t": "2020-01-01T00:01:00Z"}
        ]
      }'
python3 -m json.tool /tmp/pass_started.json

echo "== 4. 按 ID 查询凭证状态（active/cancelled/expired） =="
curl -fsS "$BASE_URL/api/passes/$PASS_ID" | python3 -m json.tool
echo "未知 ID："
curl -sS -o /tmp/pass_404.json -w 'HTTP %{http_code}\n' "$BASE_URL/api/passes/999999"
python3 -m json.tool /tmp/pass_404.json

echo "== 5. 再签一张生效凭证，用于演示区域变更约束 =="
PASS2=$(curl -fsS -X POST "$BASE_URL/api/passes" \
  -H 'Content-Type: application/json' \
  -d '{
        "points": [
          {"x": -50, "y": 200, "t": "2026-11-16T00:00:00Z"},
          {"x": 150, "y": 200, "t": "2026-11-16T00:01:00Z"}
        ]
      }')
PASS2_ID=$(echo "$PASS2" | J "['id']")
echo "第二张凭证 id=$PASS2_ID"

echo "== 6a. 区域向北扩大到 y=300（穿过两张凭证轨迹）→ 409 pass_conflict =="
REV_BEFORE=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
curl -sS -o /tmp/pass_conflict.json -w 'HTTP %{http_code}\n' \
  -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{"polygon": [[0,0],[100,0],[100,300],[0,300]]}'
python3 -m json.tool /tmp/pass_conflict.json
REV_AFTER=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "revision 变更前=$REV_BEFORE 变更后=$REV_AFTER（409 整笔回滚，revision 不变）"

echo "== 6b. 批量变更同样受凭证约束（自带轨迹安全但存量凭证冲突）→ 409 =="
curl -sS -o /tmp/pass_batch_conflict.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $REV_BEFORE,
        \"operations\": [
          {\"action\": \"update\", \"id\": $A_ID,
           \"polygon\": [[0,0],[100,0],[100,300],[0,300]]}
        ],
        \"trajectories\": [
          [{\"x\": -50, \"y\": 500, \"t\": \"2026-11-20T00:00:00Z\"},
           {\"x\": 150, \"y\": 500, \"t\": \"2026-11-20T00:01:00Z\"}]
        ]
      }"
python3 -m json.tool /tmp/pass_batch_conflict.json

echo "== 7. 取消两张凭证（第二张取消两次，结果不变） =="
curl -fsS -X POST "$BASE_URL/api/passes/$PASS_ID/cancel" \
  | python3 -c 'import sys,json; p=json.load(sys.stdin); print("首次取消:", p["status"], p["cancelled_at"])'
curl -fsS -X POST "$BASE_URL/api/passes/$PASS2_ID/cancel" >/dev/null
curl -fsS -X POST "$BASE_URL/api/passes/$PASS2_ID/cancel" \
  | python3 -c 'import sys,json; p=json.load(sys.stdin); print("重复取消仍为:", p["status"], p["cancelled_at"])'

echo "== 8. 凭证取消后区域扩大成功 =="
curl -fsS -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{"polygon": [[0,0],[100,0],[100,300],[0,300]]}' \
  | python3 -c 'import sys,json; p=json.load(sys.stdin); print("扩大成功，新 revision =", p["revision"])'

echo "完成。提示：示例数据会保留，可用 DELETE /api/regions/{id} 清理。"
