#!/usr/bin/env python
"""
gen_art.py — 文字 → 溫暖手繪動畫風的示意插圖（SDXL txt2img），給好評分享沒有圖片的評論用。

要用有 torch＋diffusers 的環境跑（目前借 /home/john/media/ghibli/venv，見 api/review_art.py 的 PYTHON）：
    .../venv/bin/python scripts/illustrate/gen_art.py jobs.json outdir

jobs.json：[{"key": "101", "prompt": "...", "seeds": [1, 2]}, ...]；每個 seed 出一張 <key>_<seed>.webp。

⚠ 只用 0002 那張卡（以序號認卡，CUDA_VISIBLE_DEVICES 只開它）：573 做影像推論會靜默打崩 runner。
  白天 0002 上還常駐 OCR 模型（約 23GB），剩約 9GB，所以用 cpu_offload（慢一點但不會擠掉 OCR）。
⚠ 風格只寫「手繪動畫、水彩、柔和光線」這類一般描述：不寫任何動畫公司或導演的名字，
  也不用拿特定作品畫面訓練的模型；人物一律是依文意想像的虛構人物，不參考任何真人照片。
"""
import json
import os
import subprocess
import sys
import time

GOOD_SERIAL = "1422019000002"
# ⚠ SDXL 的文字編碼器只讀前 77 個詞（token），後面的會被截掉：風格放最前面，場景描述要短（review_art 限 45 個英文字）
STYLE = ("hand-drawn 2D anime illustration, watercolor painted background, soft warm light, pastel colors, "
         "clean linework, cozy heartwarming, modern day Taiwan, contemporary casual clothes")
NEG = ("photo, photorealistic, realistic, 3d render, cgi, text, letters, caption, watermark, logo, signature, "
       "lowres, blurry, deformed hands, extra fingers, extra limbs, distorted face, ugly, creepy, "
       "headphones, earphones, earbuds, headset, candy, snacks, supermarket shelves, greenhouse, kimono, robe, "
       "traditional costume, nsfw")
W, H = 1216, 832           # SDXL 原生解析度裡最接近 3:2 的（官網卡片是 3:2）


def good_gpu_uuid() -> str:
    r = subprocess.run(["nvidia-smi", "--query-gpu=serial,uuid", "--format=csv,noheader"],
                       capture_output=True, text=True, timeout=15)
    for line in r.stdout.strip().splitlines():
        sn, uuid = [x.strip() for x in line.split(",")]
        if sn == GOOD_SERIAL:
            return uuid
    raise SystemExit(f"找不到序號 {GOOD_SERIAL} 的卡：\n{r.stdout}")


def main() -> int:
    jobs = json.load(open(sys.argv[1], encoding="utf-8"))
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    os.environ["CUDA_VISIBLE_DEVICES"] = good_gpu_uuid()      # 這個行程只看得到 0002
    import torch
    from diffusers import StableDiffusionXLPipeline
    t0 = time.time()
    pipe = StableDiffusionXLPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16, variant="fp16", use_safetensors=True)
    pipe.enable_model_cpu_offload(gpu_id=0)
    pipe.vae.enable_tiling()
    pipe.set_progress_bar_config(disable=True)
    print(f"模型就緒 {time.time() - t0:.0f}s，可用 VRAM {torch.cuda.mem_get_info(0)[0] / 2**30:.1f} GiB", flush=True)
    for job in jobs:
        for seed in job.get("seeds") or [1]:
            t = time.time()
            img = pipe(prompt=f"{STYLE}, {job['prompt']}", negative_prompt=NEG, width=W, height=H,
                       num_inference_steps=int(job.get("steps") or 30), guidance_scale=float(job.get("cfg") or 6.5),
                       generator=torch.Generator("cpu").manual_seed(int(seed))).images[0]
            dst = os.path.join(outdir, f"{job['key']}_{seed}.webp")
            img.save(dst, "WEBP", quality=90)
            print(json.dumps({"key": job["key"], "seed": seed, "file": dst, "sec": round(time.time() - t, 1),
                              "peak_gib": round(torch.cuda.max_memory_allocated(0) / 2**30, 2)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
