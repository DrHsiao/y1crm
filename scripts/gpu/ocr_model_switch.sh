#!/usr/bin/env bash
# 0002 好卡（:11434，表單 OCR／聽檢讀值／LINE 客戶圖片／AOI）看圖模型切換：qwen3.8 ⇄ qwen3-vl
#
#   sudo ocr_model_switch.sh qwen3-vl [--dry-run]   # 換回舊的（模型若不在 NVMe 庫，自動從 HDD 搬回）
#   sudo ocr_model_switch.sh qwen3.8  [--dry-run]   # 換成新的
#
# 2026-11-12 john「直接換成 Qwen3.8 落地取代，qwen3-vl 移到硬碟，有問題再換回」：
#   0002 改常駐 qwen3.8:27b；qwen3-vl:32b、32b-instruct 搬到 /mnt/hdd/models/ollama/models（已驗 sha256）。
#   各呼叫端都已補 "think": False（qwen3.8 預設會思考；對 qwen3-vl 無害），換回時不用動。
# 會改：y1crm config.toml／config.py、看門狗、預熱、gpu_lend.sh、AOI led_count.py／verify_vlm.py、IG ai_analysts（停用中）。
# 會重啟：y1crm.service；alphaclaw-aoi.service 只在開著時才重啟（各停幾秒）。
set -euo pipefail
case "${1:-}" in
  qwen3-vl) TO=qwen3-vl:32b-instruct; FROM=qwen3.8:27b ;;
  qwen3.8)  TO=qwen3.8:27b;           FROM=qwen3-vl:32b-instruct ;;
  *) echo "用法：$0 qwen3-vl|qwen3.8 [--dry-run]"; exit 2 ;;
esac
DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
O=http://127.0.0.1:11434
STORE=/mnt/raid1/llm_models/ollama/models
HDD=/mnt/hdd/models/ollama/models
LIB=manifests/registry.ollama.ai/library
LOG=/mnt/raid1/monitoring/ocr_model_switch.log
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG"; }
F=${FROM//./\\.}   # sed 用：點要跳脫

# 檔案｜sed 規則（只改「指定模型」那一行，不動註解與歷史紀錄）
RULES=(
  "/home/john/y1crm/config.toml|s/^model      = \"$F\"/model      = \"$TO\"/"
  "/home/john/y1crm/config.py|s/\"model\": \"$F\"/\"model\": \"$TO\"/"
  "/home/john/y1crm/scripts/gpu/ollama_watchdog.py|s/\"$F\"),/\"$TO\"),/"
  "/home/john/y1crm/scripts/gpu/ollama_warmup.sh|s/^OCR_MODEL=$F\$/OCR_MODEL=$TO/"
  "/home/john/media/ghibli/gpu_lend.sh|s/OCR_MODEL=$F /OCR_MODEL=$TO /"
  "/home/john/aoi/led_count.py|s/^VLM_MODEL = \"$F\"/VLM_MODEL = \"$TO\"/"
  "/home/john/aoi/map/verify_vlm.py|s/\"MAP_VLM_MODEL\", \"$F\"/\"MAP_VLM_MODEL\", \"$TO\"/"
  "/home/john/ig/conf/ai_analysts.json|s/\"local_model\": \"$F\"/\"local_model\": \"$TO\"/"
  "/home/john/ig/scripts/ai_analysts.py|s/c.get(\"local_model\", \"$F\")/c.get(\"local_model\", \"$TO\")/"
)

# 0. 先檢查每條規則都剛好對到一行
bad=0
for r in "${RULES[@]}"; do
  f=${r%%|*}; e=${r#*|}
  n=$(sed -n "${e}p" "$f" | wc -l)
  printf '  %-50s %s 行\n' "${f#/home/john/}" "$n"
  [ "$n" = 1 ] || bad=1
done
[ $bad = 0 ] || { log "❌ 有檔案對不到（可能已經是 $TO，或被改過），停止"; exit 1; }
[ $DRY = 1 ] && { echo "（dry-run：不改任何東西）"; exit 0; }

log "── 0002 看圖模型：$FROM → $TO"
# 1. 模型不在 NVMe 庫就從 HDD 搬回
name=${TO%%:*}; tag=${TO#*:}
if [ ! -f "$STORE/$LIB/$name/$tag" ]; then
  [ -f "$HDD/$LIB/$name/$tag" ] || { log "❌ NVMe 和 HDD 都沒有 $TO"; exit 1; }
  log "從 HDD 搬回 $TO（約 21GB，2～3 分鐘）"
  for d in $(python3 -c "import json;m=json.load(open('$HDD/$LIB/$name/$tag'));print(' '.join([m['config']['digest']]+[l['digest'] for l in m['layers']]))"); do
    b=sha256-${d#sha256:}
    [ -f "$STORE/blobs/$b" ] || cp -p "$HDD/blobs/$b" "$STORE/blobs/$b"
  done
  mkdir -p "$STORE/$LIB/$name"
  cp -p "$HDD/$LIB/$name/$tag" "$STORE/$LIB/$name/$tag"
fi
# 2. 改設定
for r in "${RULES[@]}"; do sed -i "${r#*|}" "${r%%|*}"; done
log "設定已改成 $TO"
# 3. 換卡上常駐模型（暫停看門狗 30 分）
echo '{"bases":["'$O'"],"until":'$(( $(date +%s)+1800 ))',"why":"ocr_model_switch '$TO'"}' > /run/ollama_watchdog_skip.json
curl -s -m 30 $O/api/generate -d "{\"model\":\"$FROM\",\"keep_alive\":0}" >/dev/null || true
sleep 5
curl -s -m 900 $O/api/generate -d "{\"model\":\"$TO\",\"prompt\":\"hi\",\"stream\":false,\"think\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1,\"num_ctx\":8192}}" >/dev/null
log "0002 已載入：$(curl -s $O/api/ps | python3 -c "import sys,json;print([m['name'] for m in json.load(sys.stdin)['models']])")"
# 4. 重啟用到它的服務
systemctl restart y1crm.service
systemctl try-restart alphaclaw-aoi.service   # AOI 只在本來就開著時才重啟（2026-11-12 起暫停中）
sleep 8
log "y1crm $(systemctl is-active y1crm)、AOI $(systemctl is-active alphaclaw-aoi)"
# 5. 恢復看門狗並檢查一次
rm -f /run/ollama_watchdog_skip.json
python3 /home/john/y1crm/scripts/gpu/ollama_watchdog.py
tail -3 /mnt/raid1/monitoring/ollama_watchdog.log | tee -a "$LOG"
log "── 完成"
