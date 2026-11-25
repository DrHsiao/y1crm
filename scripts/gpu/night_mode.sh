#!/usr/bin/env bash
# 夜班切換（2026-11-13 john：「01:00~06:00 可以用 qwen3.8」「請注意 3 卡溫度，不求快速，求硬體穩定」）
#
#   sudo night_mode.sh enter    # 停白天的 ollama-all（三卡 Flash-Next）→ 起 ollama-night（0002 單卡 qwen3.8）
#   sudo night_mode.sh exit     # 停工作室 → 停 ollama-night → 起 ollama-all → 預熱；起不來就先用夜間模型撐著並告警
#   sudo night_mode.sh status
#   sudo night_mode.sh boot [--dry-run]   # 開機復原（llm-boot-mode.service）：依時段＋佇列自動進夜班或回白天
#
# 夜間模型庫 /mnt/raid1/llm_models/ollama_night 裡，qwen3.8:27b 另外登記成 Flash-Next 的名稱（ollama cp），
#   所以 y1crm、Telegram、看門狗、預熱都照舊叫 qwen3.8-flash-next:125b-a6b-q4_K_M，設定一處都不用改。
# 2026-11-14 john：「以後就固定用此卡進行高效能運算」＝5717（水冷＋冷排風扇，實測 480p 零暫停、記憶體最高 60°C）
#   給電影工作室（/home/john/studio/studio_night.py）；夜間文字模型改放 0002（風冷，跑文字模型沒問題）。
# /run/llm_mode：night／day，健康檢查據此決定要看哪個容器。
set -uo pipefail
Y=/home/john/y1crm
MODE_FILE=/run/llm_mode
FN=qwen3.8-flash-next:125b-a6b-q4_K_M
G5717=GPU-e2b4896a-0069-eaa5-3b55-fb17f56d0caf
G0002=GPU-3c0a0bc4-4557-3aec-3b6a-59ef7049356e
NIGHT_GPU=$G0002                     # 夜間文字模型放這張
LOG=/mnt/raid1/monitoring/night_mode.log
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
notify(){ /usr/bin/python3 $Y/scripts/ops/notify.py "$*" >/dev/null 2>&1 || true; }
skip(){ echo '{"bases":["http://127.0.0.1:11434","http://127.0.0.1:11435","http://127.0.0.1:11436"],"until":'$(( $(date +%s) + 900 ))',"why":"night_mode '"$1"'"}' > /run/ollama_watchdog_skip.json; }
ready(){ for _ in $(seq 90); do curl -sf -m 3 http://127.0.0.1:11434/api/tags >/dev/null && return 0; sleep 2; done; return 1; }
loaded(){ curl -s -m 5 http://127.0.0.1:11434/api/ps | grep -q "$FN"; }
warm(){ bash $Y/scripts/gpu/ollama_warmup.sh >/dev/null 2>&1; loaded; }

start_night(){
  docker rm -f ollama-night >/dev/null 2>&1
  docker run -d --name ollama-night --restart no --gpus "\"device=$NIGHT_GPU\"" \
    -p 11434:11434 -p 11435:11434 -p 127.0.0.1:11436:11434 \
    -v /mnt/raid1/llm_models/ollama_night:/root/.ollama \
    -e OLLAMA_HOST=0.0.0.0:11434 -e OLLAMA_KEEP_ALIVE=-1 -e OLLAMA_MAX_LOADED_MODELS=1 -e OLLAMA_NUM_PARALLEL=2 \
    -e OLLAMA_CONTEXT_LENGTH=8192 -e OLLAMA_NOPRUNE=1 ollama/ollama:0.40.16 >/dev/null
}
start_day(){
  docker rm -f ollama-night >/dev/null 2>&1
  docker update --restart=always ollama-all >/dev/null
  docker start ollama-all >/dev/null
}

case "${1:-}" in
  enter)
    [ "$(cat $MODE_FILE 2>/dev/null)" = night ] && { log "已經是夜班"; exit 0; }
    log "── 進夜班"; skip enter
    echo night > $MODE_FILE
    docker update --restart=no ollama-all >/dev/null      # 夜裡重開機時別讓三卡容器自己起來搶卡
    docker stop ollama-all >/dev/null
    start_night
    if ready && warm; then
      log "夜間模型（qwen3.8，0002 單卡）就緒；5717 已空出給工作室"
    else
      log "❌ 夜間模型起不來 → 退回白天，今晚工作室不開"
      start_day; echo day > $MODE_FILE; ready && warm
      rm -f /run/ollama_watchdog_skip.json
      notify "⚠️ 夜班：夜間模型（qwen3.8）起不來，已退回白天三卡模式，今晚電影工作室不開"
      exit 1
    fi
    rm -f /run/ollama_watchdog_skip.json
    python3 $Y/scripts/gpu/ollama_watchdog.py >/dev/null 2>&1
    nvidia-smi --query-gpu=serial,memory.used,temperature.gpu --format=csv,noheader | sed 's/^/  /' | tee -a "$LOG" ;;
  exit)
    [ "$(cat $MODE_FILE 2>/dev/null)" = night ] || { log "不是夜班，不用切"; exit 0; }
    log "── 回白天"; skip exit
    systemctl stop studio-night.service 2>/dev/null || true     # 工作室先收（它自己也會在期限前收）
    pkill -f "ComfyUI/main.py" 2>/dev/null || true; sleep 5
    start_day
    if ready && warm; then
      echo day > $MODE_FILE; log "白天三卡 Flash-Next 就緒"
    else
      log "❌ 三卡模型起不來 → 先用夜間單卡 qwen3.8 撐著"
      docker update --restart=no ollama-all >/dev/null; docker stop ollama-all >/dev/null
      start_night; ready && warm
      notify "⚠️ 早上三卡 Flash-Next 起不來，先用夜間單卡 qwen3.8 撐著（LINE 客服照常），請查看 /mnt/raid1/monitoring/night_mode.log"
    fi
    rm -f /run/ollama_watchdog_skip.json
    python3 $Y/scripts/gpu/ollama_watchdog.py >/dev/null 2>&1
    tail -3 /mnt/raid1/monitoring/ollama_watchdog.log | tee -a "$LOG" ;;
  boot)
    # 開機復原（2026-11-16 john 同意；llm-boot-mode.service 開機時跑）：
    #   夜班時 ollama-all、ollama-night 都是 restart=no，/run/llm_mode 又在 tmpfs → 夜班中重開機＝三個埠都沒模型，
    #   而且 08:00 的 exit 會因為「不是夜班」直接結束 → 整個白天也沒模型（11-16 00:00、00:53 兩次停電都靠人工救）。
    #   判斷照工作室開工檢查（時段內、離收工 >10 分、佇列有工作）：通過 → 進夜班（只換模型，工作室等下一個整點排程）；
    #   否則 → 白天三卡。  boot --dry-run：只印判斷，不動容器。
    DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
    if [ $DRY = 0 ] && [ -s $MODE_FILE ]; then log "開機復原：模式已是 $(cat $MODE_FILE)，不動"; exit 0; fi
    if /home/john/studio/venv/bin/python /home/john/studio/studio_night.py --check; then
      [ $DRY = 1 ] && { echo "判斷：進夜班（工作室時段內、佇列有工作）"; exit 0; }
      log "開機復原：工作室時段內、佇列有工作 → 進夜班（工作室等下一個整點排程開工）"
      "$0" enter; rc=$?
      [ $rc = 0 ] && notify "🔌 重開機後已自動進夜班（0002 單卡 qwen3.8 撐客服／OCR）；電影工作室等下一個整點排程接著做"
      exit $rc
    fi
    [ $DRY = 1 ] && { echo "判斷：白天三卡（不在工作室時段、離收工 ≤10 分，或佇列沒工作）"; exit 0; }
    log "開機復原：不在工作室時段（或佇列沒工作）→ 白天三卡"; skip boot
    start_day
    if ready && warm; then
      echo day > $MODE_FILE; log "開機復原：白天三卡 Flash-Next 就緒"
    else
      echo day > $MODE_FILE; log "❌ 開機復原：三卡模型起不來"
      notify "⚠️ 重開機後三卡 Flash-Next 起不來，請查看 /mnt/raid1/monitoring/night_mode.log"
    fi
    rm -f /run/ollama_watchdog_skip.json ;;
  status)
    echo "模式：$(cat $MODE_FILE 2>/dev/null || echo 未設定（白天）)"
    docker ps --format '{{.Names}} {{.Status}}' | grep -E "ollama|searxng"
    nvidia-smi --query-gpu=serial,memory.used,temperature.gpu,power.draw,power.limit --format=csv,noheader ;;
  *) echo "用法：$0 enter|exit|boot [--dry-run]|status"; exit 2 ;;
esac
