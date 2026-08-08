#!/usr/bin/env bash
# ollama_warmup.sh — 開機後把兩個模型預先載進 VRAM
#
# 為什麼需要：ollama 的 KEEP_ALIVE=-1 只保證「已載入的不被卸載」，重開機後 VRAM 是空的。
# 模型 blob 在 HDD RAID1（~150MB/s），32B 冷載入約 7 分半。若不預熱：
#   - 店員第一次跑表單 OCR 會等 7 分鐘，像當機；
#   - LINE 客服 AI 只有 30 秒預算 → 重開機後第一位客戶必定收到道歉文，
#     而且該對話會被自動轉人工、4 小時內 AI 不再回應（靜默發生，沒人會發現）。
#
# 兩張卡各自獨立，故兩邊並行預熱（各約 7 分半，不是相加）。
# 可隨時手動重跑；模型已在 VRAM 時會秒回。
set -uo pipefail

OCR_URL=http://127.0.0.1:11434      # 好卡 0002：影像（表單 OCR、客戶圖片）
AI_URL=http://127.0.0.1:11435       # 573：純文字（LINE 客服 AI）⚠ 絕不可送影像
OCR_MODEL=qwen3-vl:32b-instruct
AI_MODEL=qwen3:32b
LOG=/mnt/raid1/monitoring/ollama_warmup.log

log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

wait_up() {   # 等 ollama API 有回應（docker 剛起來時會有幾秒空窗）
  local url=$1 n=0
  until curl -sf -m 5 "$url/api/tags" >/dev/null 2>&1; do
    n=$((n+1)); [ "$n" -gt 60 ] && { log "❌ $url 逾時未就緒"; return 1; }
    sleep 5
  done
}

warm() {      # $1=名稱 $2=URL $3=模型
  local tag=$1 url=$2 model=$3 t0=$SECONDS
  wait_up "$url" || return 1
  # num_predict=1：只要觸發載入即可，不需要真的生成內容
  if curl -sf -m 1200 "$url/api/generate" -H 'Content-Type: application/json' \
       -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1,
            \"options\":{\"num_predict\":1}}" >/dev/null; then
    log "✅ $tag $model 已載入（$((SECONDS-t0))s）"
  else
    log "❌ $tag $model 載入失敗（$((SECONDS-t0))s）"
    return 1
  fi
}

mkdir -p "$(dirname "$LOG")"
log "── 開始預熱 ──"
warm "OCR/好卡" "$OCR_URL" "$OCR_MODEL" &
p1=$!
warm "客服AI/573" "$AI_URL" "$AI_MODEL" &
p2=$!
wait $p1; r1=$?
wait $p2; r2=$?
log "── 預熱結束（OCR=$r1 AI=$r2）──"
exit $(( r1 || r2 ))
