#!/usr/bin/env bash
# ollama 模型在 NVMe 模型庫 ⇄ 備份碟（HDD）之間搬移，保持 ollama 的目錄結構（manifests／blobs）。
#
#   sudo ollama_hdd.sh backup  <模型:標籤>...   複製到 HDD → 逐一驗 sha256 → 全部對了才從 NVMe 刪（ollama rm）
#   sudo ollama_hdd.sh restore <模型:標籤>...   從 HDD 複製回 NVMe（HDD 那份保留）；已在 NVMe 的跳過
#   sudo ollama_hdd.sh list                      列出 HDD 上有哪些模型
#
# 2026-11-12 john：「Qwen3.8-Flash-Next 留下來，其餘模型先移到備份碟」。
# ⚠ ollama rm 只刪沒有其他模型引用的 blob，共用的 blob（例如授權、範本）不會被刪。
# ⚠ 三卡合跑模式下，呼叫端用到不在 NVMe 的模型名稱會直接報「找不到模型」，不會擠掉 Flash-Next（這是好事）。
set -euo pipefail
STORE=/mnt/raid1/llm_models/ollama/models
HDD=/mnt/hdd/models/ollama/models
LOG=/mnt/raid1/monitoring/ollama_hdd.log
CTR=${OLLAMA_CTR:-$(docker ps --format '{{.Names}}' | grep -E '^(ollama-all|ollama)$' | head -1)}
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

mpath() {   # 模型:標籤 → manifests 底下的相對路徑
  local m=$1 name tag
  name=${m%:*}; tag=${m##*:}; [ "$name" = "$m" ] && tag=latest
  case "$name" in
    */*/*) echo "$name/$tag" ;;                          # hf.co/作者/倉庫
    */*)   echo "registry.ollama.ai/$name/$tag" ;;
    *)     echo "registry.ollama.ai/library/$name/$tag" ;;
  esac
}
digests() { python3 -c "import json,sys;m=json.load(open(sys.argv[1]));print(' '.join([m['config']['digest']]+[l['digest'] for l in m['layers']]))" "$1"; }

copy_model() {   # $1=來源庫 $2=目的庫 $3=模型；複製並驗證新複製的 blob
  local src=$1 dst=$2 m=$3 rel d b
  rel=$(mpath "$m")
  [ -f "$src/manifests/$rel" ] || { log "❌ $m：來源沒有 manifest（$src）"; return 1; }
  mkdir -p "$dst/blobs" "$(dirname "$dst/manifests/$rel")"
  for d in $(digests "$src/manifests/$rel"); do
    b=sha256-${d#sha256:}
    if [ -f "$dst/blobs/$b" ] && [ "$(stat -c %s "$dst/blobs/$b")" = "$(stat -c %s "$src/blobs/$b")" ]; then
      continue
    fi
    cp -p "$src/blobs/$b" "$dst/blobs/$b.tmp"
    [ "$(sha256sum "$dst/blobs/$b.tmp" | cut -d' ' -f1)" = "${d#sha256:}" ] || { rm -f "$dst/blobs/$b.tmp"; log "❌ $m：$b 驗證失敗"; return 1; }
    mv "$dst/blobs/$b.tmp" "$dst/blobs/$b"
  done
  cp -p "$src/manifests/$rel" "$dst/manifests/$rel"
  log "✅ $m 已複製並驗證（$(du -ch $(for d in $(digests "$dst/manifests/$rel"); do echo "$dst/blobs/sha256-${d#sha256:}"; done) | tail -1 | cut -f1)）"
}

case "${1:-}" in
  backup)
    shift; ok=()
    for m in "$@"; do copy_model "$STORE" "$HDD" "$m" && ok+=("$m"); done
    [ ${#ok[@]} = $# ] || { log "❌ 有模型沒備份成功，全部不刪"; exit 1; }
    chown -R john:john /mnt/hdd/models/ollama
    for m in "${ok[@]}"; do docker exec "$CTR" ollama rm "$m" >/dev/null && log "🗑 NVMe 已刪 $m"; done
    df -h /mnt/raid1 /mnt/hdd | tail -2 | tee -a "$LOG" ;;
  restore)
    shift
    for m in "$@"; do
      if [ -f "$STORE/manifests/$(mpath "$m")" ]; then log "$m 已在 NVMe，跳過"; continue; fi
      log "從 HDD 搬回 $m"; copy_model "$HDD" "$STORE" "$m"
    done ;;
  list)
    cd "$HDD/manifests" && find . -type f | sed 's|^\./||; s|registry.ollama.ai/library/||; s|/\([^/]*\)$|:\1|' | sort ;;
  *) echo "用法：$0 backup|restore <模型:標籤>... ｜ $0 list"; exit 2 ;;
esac
