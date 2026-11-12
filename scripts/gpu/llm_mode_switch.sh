#!/usr/bin/env bash
# 本機模型配置切換：三卡各一顆（split）⇄ 三卡合跑一顆 Qwen3.8-Flash-Next（flashnext）
#
#   sudo llm_mode_switch.sh flashnext [--dry-run]   # 停三個 ollama 容器，改開 ollama-all（三卡），
#                                                   # 11434／11435／11436 三個埠都指向它，所有呼叫端改用同一顆模型
#   sudo llm_mode_switch.sh split     [--dry-run]   # 換回：0002 qwen3.8（看圖）、573 qwen3:32b（客服）、5717 qwen3.6（分析）
#                                                   #   三顆模型若在備份碟，會先自動搬回 NVMe
#
# 2026-11-12 john：「好，開始下載，完成後直接取代工作」「主記憶體和顯卡記憶體的容量請最佳化配置」。
# 調整參數（環境變數，flashnext 用）：
#   FN_NP     平行請求槽數（預設 3：客服／看圖／推理互不排隊）
#   FN_CTX    每槽上下文（預設 8192；所有呼叫端都傳 8192，不同會逼 ollama 重載整顆 120GB）
#   FN_EXTRA  額外 llama-server 參數（經包裝程式附加）。預設＝11-12 實測最佳：
#             -lzm off -lm mlock：35GB 逐層 N-gram 詞表常駐並鎖在主記憶體（隨用隨讀只有 34～38 t/s，鎖定 48 t/s）
#             -ts 30,35,35：減輕 5717（CUDA0，冷排沒風扇）的份量
#             設成空字串＝不掛包裝程式、用 ollama 預設配置
# ⚠ workspace/docker-compose.yml 仍定義三個舊容器；flashnext 模式下不要 `docker compose up -d`（會搶埠）。
set -euo pipefail
FN_EXTRA=${FN_EXTRA-"-lzm off -lm mlock -ts 30,35,35"}
MODE=${1:-}; DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
FN=qwen3.8-flash-next:125b-a6b-q4_K_M
OCR=qwen3.8:27b; AI=qwen3:32b; ANA=qwen3.6:35b-a3b
case "$MODE" in
  flashnext) TO_OCR=$FN;  TO_AI=$FN; TO_ANA=$FN;  FR_OCR=$OCR; FR_AI=$AI; FR_ANA=$ANA
             TO_CTR='CONTAINERS = ["ollama-all"]'; FR_CTR='CONTAINERS = \["ollama", "ollama-573", "ollama-5717"\]' ;;
  split)     TO_OCR=$OCR; TO_AI=$AI; TO_ANA=$ANA; FR_OCR=$FN;  FR_AI=$FN; FR_ANA=$FN
             TO_CTR='CONTAINERS = ["ollama", "ollama-573", "ollama-5717"]'; FR_CTR='CONTAINERS = \["ollama-all"\]' ;;
  *) echo "用法：$0 flashnext|split [--dry-run]"; exit 2 ;;
esac
e(){ printf '%s' "$1" | sed 's/[.[\*^$/]/\\&/g'; }   # sed 跳脫
LOG=/mnt/raid1/monitoring/llm_mode_switch.log
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
Y=/home/john/y1crm
STORE=/mnt/raid1/llm_models/ollama
G0002=GPU-3c0a0bc4-4557-3aec-3b6a-59ef7049356e; G573=GPU-81a8a467-94fc-cef7-3d6d-846892c2f864; G5717=GPU-e2b4896a-0069-eaa5-3b55-fb17f56d0caf
WRAP=/mnt/raid1/llm_models/llama-server-wrapper.sh; REAL=/mnt/raid1/llm_models/llama-server.real

# 檔案｜sed 規則（每條必須剛好對到一行）
RULES=(
  "$Y/config.toml|s/^model      = \"$(e $FR_OCR)\"/model      = \"$TO_OCR\"/"
  "$Y/config.toml|s/^model             = \"$(e $FR_AI)\"/model             = \"$TO_AI\"/"
  "$Y/config.py|/\"ollama_url\": \"http:\/\/127.0.0.1:11434\"/,/\"model\"/ s/\"model\": \"$(e $FR_OCR)\"/\"model\": \"$TO_OCR\"/"
  "$Y/config.py|/\"url\": \"http:\/\/127.0.0.1:11435\"/,/\"model\"/ s/\"model\": \"$(e $FR_AI)\"/\"model\": \"$TO_AI\"/"
  "$Y/scripts/gpu/ollama_watchdog.py|/OCR\/好卡0002/ s/\"$(e $FR_OCR)\")/\"$TO_OCR\")/"
  "$Y/scripts/gpu/ollama_watchdog.py|/客服AI\/573/ s/\"$(e $FR_AI)\")/\"$TO_AI\")/"
  "$Y/scripts/gpu/ollama_watchdog.py|/分析\/5717/ s/\"$(e $FR_ANA)\")/\"$TO_ANA\")/"
  "$Y/scripts/gpu/ollama_warmup.sh|s/^OCR_MODEL=$(e $FR_OCR)\$/OCR_MODEL=$TO_OCR/"
  "$Y/scripts/gpu/ollama_warmup.sh|s/^AI_MODEL=$(e $FR_AI)\$/AI_MODEL=$TO_AI/"
  "$Y/scripts/gpu/ollama_warmup.sh|s/^ANA_MODEL=$(e $FR_ANA)\$/ANA_MODEL=$TO_ANA/"
  "$Y/scripts/ops/healthcheck.py|s/^$FR_CTR/$TO_CTR/"
  "/home/john/workspace/utils/agent_brain.py|s/self._fallback_model = \"$(e $FR_AI)\"/self._fallback_model = \"$TO_AI\"/"
  "/home/john/yuanta/yt_ask.py|s/^MODEL = \"$(e $FR_AI)\"/MODEL = \"$TO_AI\"/"
  "/home/john/aoi/led_count.py|s/^VLM_MODEL = \"$(e $FR_OCR)\"/VLM_MODEL = \"$TO_OCR\"/"
  "/home/john/aoi/led_count.py|s/^LLM_MODEL = \"$(e $FR_AI)\"/LLM_MODEL = \"$TO_AI\"/"
  "/home/john/aoi/map/verify_vlm.py|s/\"MAP_VLM_MODEL\", \"$(e $FR_OCR)\"/\"MAP_VLM_MODEL\", \"$TO_OCR\"/"
  "/home/john/ig/conf/ai_analysts.json|s/\"local_model\": \"$(e $FR_OCR)\"/\"local_model\": \"$TO_OCR\"/"
  "/home/john/ig/scripts/ai_analysts.py|s/c.get(\"local_model\", \"$(e $FR_OCR)\")/c.get(\"local_model\", \"$TO_OCR\")/"
)
bad=0
for r in "${RULES[@]}"; do
  f=${r%%|*}; x=${r#*|}
  n=$(sed -n "${x}p" "$f" | wc -l)
  printf '  %-48s %s 行\n' "${f#/home/john/}" "$n"
  [ "$n" = 1 ] || bad=1
done
[ $bad = 0 ] || { log "❌ 有規則沒對到剛好一行（可能已經是 $MODE 模式），停止"; exit 1; }
[ -f "$STORE/models/manifests/registry.ollama.ai/library/qwen3.8-flash-next/125b-a6b-q4_K_M" ] || { log "❌ 模型還沒下載完"; exit 1; }
[ $DRY = 1 ] && { echo "（dry-run：不改任何東西）"; exit 0; }

log "── 切換到 $MODE"
SKIP='{"bases":["http://127.0.0.1:11434","http://127.0.0.1:11435","http://127.0.0.1:11436"],"until":'$(( $(date +%s)+3600 ))',"why":"llm_mode_switch '$MODE'"}'
echo "$SKIP" > /run/ollama_watchdog_skip.json          # 先停看門狗，再動任何東西
for r in "${RULES[@]}"; do sed -i "${r#*|}" "${r%%|*}"; done
log "設定已改成 $MODE"

if [ "$MODE" = flashnext ]; then
  docker update --restart=no ollama ollama-573 ollama-5717 >/dev/null
  docker stop ollama ollama-573 ollama-5717 >/dev/null
  docker rm -f ollama-all >/dev/null 2>&1 || true
  EXTRA_MOUNT=()
  if [ -n "${FN_EXTRA:-}" ]; then
    [ -f "$REAL" ] || docker run --rm --entrypoint cat ollama/ollama:0.40.16 /usr/lib/ollama/llama-server > "$REAL"
    chmod 755 "$REAL"
    printf '#!/bin/sh\nexec /usr/lib/ollama/llama-server.real "$@" $LLAMA_EXTRA_ARGS\n' > "$WRAP"; chmod 755 "$WRAP"
    EXTRA_MOUNT=(-v "$REAL:/usr/lib/ollama/llama-server.real:ro" -v "$WRAP:/usr/lib/ollama/llama-server:ro" -e "LLAMA_EXTRA_ARGS=$FN_EXTRA")
  fi
  docker run -d --name ollama-all --restart always --gpus "\"device=$G0002,$G573,$G5717\"" \
    -p 11434:11434 -p 11435:11434 -p 127.0.0.1:11436:11434 -v $STORE:/root/.ollama \
    -e OLLAMA_HOST=0.0.0.0:11434 -e OLLAMA_KEEP_ALIVE=-1 -e OLLAMA_MAX_LOADED_MODELS=1 \
    -e OLLAMA_NUM_PARALLEL=${FN_NP:-3} -e OLLAMA_CONTEXT_LENGTH=${FN_CTX:-8192} -e OLLAMA_NOPRUNE=1 -e OLLAMA_SCHED_SPREAD=1 --ulimit memlock=-1:-1 --cap-add IPC_LOCK \
    "${EXTRA_MOUNT[@]}" ollama/ollama:0.40.16 >/dev/null
  log "ollama-all 已啟動（NP=${FN_NP:-3} CTX=${FN_CTX:-8192} EXTRA='${FN_EXTRA:-}'）"
else
  # 2026-11-12 起其餘模型都搬到備份碟（ollama_hdd.sh）；換回前先把三顆搬回 NVMe（已在的跳過，約 5～8 分鐘）
  bash $Y/scripts/gpu/ollama_hdd.sh restore "$OCR" "$AI" "$ANA"
  docker rm -f ollama-all >/dev/null 2>&1 || true
  docker update --restart=always ollama ollama-573 ollama-5717 >/dev/null
  docker start ollama ollama-573 ollama-5717 >/dev/null
  log "三個容器已啟動"
fi
for i in $(seq 60); do curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 2; done
bash $Y/scripts/gpu/ollama_warmup.sh; log "warmup RC=$?"
systemctl restart y1crm.service openclaw-telegram.service
systemctl try-restart alphaclaw-aoi.service
sleep 6
log "y1crm $(systemctl is-active y1crm)、telegram $(systemctl is-active openclaw-telegram)"
rm -f /run/ollama_watchdog_skip.json
python3 $Y/scripts/gpu/ollama_watchdog.py; tail -3 /mnt/raid1/monitoring/ollama_watchdog.log | tee -a "$LOG"
log "── 完成 $MODE"
