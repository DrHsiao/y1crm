#!/usr/bin/env python
"""
gpu573_torch_stage.py — gpu573_image_test.py 的 A、B 段（要在有 torch＋diffusers 的環境跑，借 ghibli venv）。
呼叫端先設好 CUDA_VISIBLE_DEVICES＝要測的那張卡的 UUID（這個行程只看得到那一張）。

    python -I gpu573_torch_stage.py compute <秒數>        # A：fp16 矩陣乘＋主機↔顯卡搬資料，逐次比對
    python -I gpu573_torch_stage.py sdxl <輸出資料夾> <seed>…   # B：SDXL 畫圖（cpu_offload＝每一步都在 PCIe 上搬權重）

每一步印一行 JSON（呼叫端讀 stdout 記錄、判斷）。
"""
import hashlib
import json
import os
import sys
import time


def out(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def compute(seconds: float) -> int:
    import torch
    dev = torch.device("cuda")
    out(stage="compute", event="start", gpu=torch.cuda.get_device_name(0),
        free_gib=round(torch.cuda.mem_get_info(0)[0] / 2**30, 1))
    g = torch.Generator(device="cpu").manual_seed(573)
    a = torch.randn(8192, 8192, generator=g).half().to(dev)
    b = torch.randn(8192, 8192, generator=g).half().to(dev)
    ref = a @ b
    torch.cuda.synchronize()
    ref_hash = hashlib.sha256(ref.cpu().numpy().tobytes()).hexdigest()[:16]
    out(stage="compute", event="ref", hash=ref_hash)
    # 主機↔顯卡搬資料：2GB 釘住記憶體來回搬，每次比對雜湊（測 PCIe x4 在大量傳輸下會不會出錯）
    host = torch.randint(0, 255, (2 * 1024**3,), dtype=torch.uint8, generator=g).pin_memory()
    host_hash = hashlib.sha256(host.numpy().tobytes()).hexdigest()[:16]
    t0, n_mm, bad_mm, n_xfer, bad_xfer = time.time(), 0, 0, 0, 0
    while time.time() - t0 < seconds:
        for _ in range(200):               # 一次約 11ms，200 次讓卡真的吃滿
            c = a @ b
            n_mm += 1
            if not torch.equal(c, ref):
                bad_mm += 1
        torch.cuda.synchronize()
        d = host.to(dev, non_blocking=True)
        back = d.to("cpu")
        torch.cuda.synchronize()
        n_xfer += 1
        if hashlib.sha256(back.numpy().tobytes()).hexdigest()[:16] != host_hash:
            bad_xfer += 1
        del d, back
        out(stage="compute", event="progress", sec=round(time.time() - t0, 1), matmul=n_mm, matmul_bad=bad_mm,
            transfer_2gb=n_xfer, transfer_bad=bad_xfer)
    out(stage="compute", event="done", matmul=n_mm, matmul_bad=bad_mm, transfer_2gb=n_xfer, transfer_bad=bad_xfer,
        hash=ref_hash)
    return 0 if not (bad_mm or bad_xfer) else 2


def sdxl(outdir: str, seeds) -> int:
    import torch
    from diffusers import StableDiffusionXLPipeline
    os.makedirs(outdir, exist_ok=True)
    t0 = time.time()
    pipe = StableDiffusionXLPipeline.from_pretrained(
        "stabilityai/stable-diffusion-xl-base-1.0", torch_dtype=torch.float16, variant="fp16", use_safetensors=True)
    pipe.enable_model_cpu_offload(gpu_id=0)
    pipe.vae.enable_tiling()
    pipe.set_progress_bar_config(disable=True)
    out(stage="sdxl", event="loaded", sec=round(time.time() - t0, 1), gpu=torch.cuda.get_device_name(0))
    prompt = "hand-drawn 2D anime illustration, watercolor painted background, a quiet park with trees and a bench, soft light"
    for seed in seeds:
        t = time.time()
        img = pipe(prompt=prompt, negative_prompt="text, watermark, photo", width=1024, height=1024, num_inference_steps=30,
                   guidance_scale=6.5, generator=torch.Generator("cpu").manual_seed(int(seed))).images[0]
        path = os.path.join(outdir, f"seed{seed}.png")
        img.save(path)
        out(stage="sdxl", event="image", seed=int(seed), sec=round(time.time() - t, 1), file=path,
            hash=hashlib.sha256(img.tobytes()).hexdigest()[:16],
            peak_gib=round(torch.cuda.max_memory_allocated(0) / 2**30, 2))
    out(stage="sdxl", event="done")
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "compute":
        sys.exit(compute(float(sys.argv[2])))
    sys.exit(sdxl(sys.argv[2], sys.argv[3:]))
