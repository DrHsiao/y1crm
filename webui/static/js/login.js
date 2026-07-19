/* login.js — 登入頁（獨立頁，不載 common.js）
   成功後導回 ?next=（預設 /calendar）。 */
"use strict";

const $ = id => document.getElementById(id);
const NEXT = new URLSearchParams(location.search).get("next") || "/calendar";

// 商標資訊
fetch("/api/branding", { credentials: "same-origin" })
  .then(r => r.json())
  .then(b => {
    $("login-company").textContent = b.company_name || "y1crm";
    $("login-sub").textContent = b.branch_name ? `${b.branch_name}‧請登入使用` : "請登入使用";
  }).catch(() => { $("login-company").textContent = "y1crm"; });

async function login() {
  $("login-error").classList.add("hidden");
  try {
    const res = await fetch("/api/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-username").value,
                             password: $("login-password").value }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    location.href = NEXT;
  } catch (e) {
    $("login-error").textContent = e.message;
    $("login-error").classList.remove("hidden");
  }
}
$("login-btn").onclick = login;
$("login-password").addEventListener("keydown", e => { if (e.key === "Enter") login(); });
$("login-username").addEventListener("keydown", e => { if (e.key === "Enter") $("login-password").focus(); });
$("pw-toggle").onclick = () => {
  const i = $("login-password"), show = i.type === "password";
  i.type = show ? "text" : "password";
  $("pw-toggle").textContent = show ? "隱藏" : "顯示";
};

// 已登入就直接進系統（避免多此一舉再登一次）
fetch("/api/me", { credentials: "same-origin" })
  .then(r => { if (r.ok) location.replace(NEXT); }).catch(() => {});

// ── 登入頁動態小點特效（參考 antigravity.google）─────────────
function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
try {
  (() => {
    const cv = $("login-fx"), ctx = cv.getContext("2d");
    const GAP = 36, WOBBLE = 3, R_MOUSE = 150, PUSH = 30;
    let dots = [], W = 0, H = 0;
    const mouse = { x: -1e4, y: -1e4 };

    function build() {
      const dpr = Math.min(devicePixelRatio || 1, 2);
      W = innerWidth; H = innerHeight;
      cv.width = W * dpr; cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      dots = [];
      for (let y = GAP / 2; y < H; y += GAP)
        for (let x = GAP / 2; x < W; x += GAP)
          dots.push({ x, y, r: 1.1 + Math.random() * 1.2,
                      p: Math.random() * Math.PI * 2,
                      s: .35 + Math.random() * .5,
                      dx: 0, dy: 0 });
    }
    addEventListener("resize", build);
    addEventListener("pointermove", e => { mouse.x = e.clientX; mouse.y = e.clientY; });
    document.documentElement.addEventListener("mouseleave", () => { mouse.x = mouse.y = -1e4; });

    function frame(t) {
      requestAnimationFrame(frame);
      const col = currentTheme() === "dark" ? "213,37,63" : "200,16,46";  // 企業紅
      const tt = t / 1000;
      ctx.clearRect(0, 0, W, H);
      for (const d of dots) {
        const wx = Math.cos(tt * d.s + d.p) * WOBBLE;
        const wy = Math.sin(tt * d.s * 1.3 + d.p) * WOBBLE;
        const mx = d.x + wx - mouse.x, my = d.y + wy - mouse.y;
        const dist = Math.hypot(mx, my);
        let tx = 0, ty = 0, boost = 0;
        if (dist < R_MOUSE && dist > .01) {
          boost = 1 - dist / R_MOUSE;
          tx = mx / dist * boost * PUSH;
          ty = my / dist * boost * PUSH;
        }
        d.dx += (tx - d.dx) * .08;
        d.dy += (ty - d.dy) * .08;
        const a = Math.min(.18 + .12 * Math.sin(tt * .7 + d.p * 2) + boost * .6, .9);
        ctx.beginPath();
        ctx.arc(d.x + wx + d.dx, d.y + wy + d.dy, d.r + boost * 1.4, 0, 7);
        ctx.fillStyle = `rgba(${col},${a.toFixed(3)})`;
        ctx.fill();
      }
    }
    build();
    requestAnimationFrame(frame);
  })();
} catch (e) { console.warn("login-fx 特效停用：", e); }
