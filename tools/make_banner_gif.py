# -*- coding: utf-8 -*-
"""生成 zabanbanner.gif:直接截取官网 hero 区域,所见即所得全面对齐官网。

加载本地 index.html 本尊(自动起 http.server),逐帧显式驱动网站自己的
动画时间线(聊天卡打字/消息淡入/光标、按钮蹦猫彩蛋),截 .hero 区域合成 GIF。
聊天卡是顺应页面配色特调的,必须网页实录;只有右侧走路猫不走浏览器渲染
——录屏时隐藏,合成阶段用 PIL 把 zabanwalk.gif 原帧贴回原位(浏览器对它
的重绘/光栅化抖动会产生重影)。静态区帧间差分置透明(disposal=1)控制体积。

用法: python tools/make_banner_gif.py   → 输出 zabanbanner.gif(覆盖前备份 .bak)
"""
from __future__ import annotations

import base64
import bisect
import io
import json
import random
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "zabanbanner.gif"
PORT = 8807
URL = f"http://localhost:{PORT}"
LOOP_MS = 6900   # 网站 chatAnim 动画周期
FRAME_MS = 100   # 与走路动画 100ms/帧严格对齐:节奏错位会让两个姿势视觉上糊成重影
SCALE = 2        # 2x 渲染(device_scale_factor),输出更清晰的大图
SPRITE_GUTTER = 8  # 雪碧图帧间透明隔离带,杜绝浏览器采样渗入相邻帧

# 与 index.html CAT_FACES 一致
CAT_FACES = ["(=^ω^=)", "(=•ω•=)", "(=˙ᆺ˙=)", "∧,,,∧", "(=ＴωＴ=)", "(=｀ω´=)", "/ᐠ｡ꞈ｡ᐟ\\",
             "(っ`ω´c)", "꒰ঌ(っ˘꒳˘ｃ)໒꒱", "꜀(^. .^꜀ )꜆੭", "ฅ^･֊･^ฅ", "(っ`ᵕ´c)!", ":3c"]

# 与 index.html chatAnim 时间线一致(同 capture_chat_gif.py);
# 蹦猫复刻 catPop + cat-jump keyframes(0/15/55/100%)
SET_STATE_JS = """
(t) => {
  const mom   = document.getElementById('chat-msg-mom');
  const bot   = document.getElementById('chat-msg-bot');
  const typed = document.getElementById('chat-typed');
  const caret = document.getElementById('chat-caret');
  const ph    = document.getElementById('chat-placeholder');
  let txt = '', phVis = true;
  if (t >= 2100 && t < 2450) { txt = '咋'; phVis = false; }
  else if (t >= 2450 && t < 3150) { txt = '咋办'; phVis = false; }
  typed.textContent = txt;
  ph.style.display = phVis ? '' : 'none';
  // 消息直接整条出现,不做透明度渐变:10fps 下淡入的中间帧会把
  // blurple ✓APP 徽章混成紫色(半透明蓝紫叠深棕底),观感像"变色"
  const fade = (el, t0) => { el.style.opacity = t < t0 ? '0' : '1'; el.style.transform = 'none'; };
  fade(mom, 3150);
  fade(bot, 3900);
  caret.style.opacity = (t % 1060) < 520 ? '1' : '0';
  const fi = Math.floor(t / WALK_FRAME_MS) % WALK_N;
  document.documentElement.style.setProperty('--walk-av', `${-fi * AV_STEP}px`);
  const POPS = __POPS__;
  for (let i = 0; i < POPS.length; i++) {
    const p = POPS[i];
    const s = document.getElementById('pop-' + i);
    const q = (t - p.t0) / 850;
    if (q < 0 || q >= 1) { s.style.opacity = '0'; continue; }
    const e1 = Math.min(q / 0.55, 1);
    const eo = 1 - Math.pow(1 - e1, 2);          // 近似 cubic-bezier(.25,.6,.4,1)
    const e2 = Math.max((q - 0.55) / 0.45, 0);
    const op = q < 0.15 ? q / 0.15 : (q < 0.55 ? 1 : 1 - e2);
    const x  = p.dx   * (q < 0.55 ? eo : 1 + 0.5 * e2);
    const y  = p.peak * (q < 0.55 ? eo : 1 - 0.7 * e2);
    const sc = q < 0.55 ? 0.5 + 0.5 * eo : 1 - 0.1 * e2;
    const rot = p.rot * (q < 0.55 ? eo : 1);
    s.style.opacity = op.toFixed(3);
    s.style.transform = `translate(calc(-50% + ${x.toFixed(1)}px), ${y.toFixed(1)}px) ` +
                        `rotate(${rot.toFixed(1)}deg) scale(${sc.toFixed(3)})`;
  }
}
"""


def make_pops() -> list[dict]:
    """蹦猫时间表:两阵齐蹦,每个时间点三个按钮各蹦一只,参数分布同官网 catPop()。"""
    rng = random.Random("zaban-pop")
    pops = []
    for t_start, t_end in ((1200, 2300), (4600, 5400)):
        for t0 in range(t_start, t_end, 180):
            for btn in (0, 1, 2):
                pops.append({
                    "t0": t0, "btn": btn, "face": rng.choice(CAT_FACES),
                    "left": round(20 + rng.random() * 60),
                    "dx": round(rng.random() * 90 - 45),
                    "peak": -round(28 + rng.random() * 38),
                    "rot": round(rng.random() * 44 - 22),
                })
    return pops


def walk_sprite(target_h: int) -> tuple[str, int, int, int]:
    """zabanwalk.gif -> 预缩放到 target_h 高的雪碧图 data URI(浏览器零缩放,
    无重采样就不会把相邻帧的描边渗进来)。返回 (uri, 帧宽, 帧数, 单帧ms)。"""
    gif = Image.open(ROOT / "zabanwalk.gif")
    frames = []
    for i in range(gif.n_frames):
        gif.seek(i)
        f = gif.convert("RGBA")
        fw = round(target_h * f.width / f.height)
        # BILINEAR 模拟浏览器的平滑缩放;LANCZOS 振铃会给黑描边镶一圈"双边"
        frames.append(f.resize((fw, target_h), Image.BILINEAR))
    n, fw = len(frames), frames[0].width
    step = fw + SPRITE_GUTTER
    sheet = Image.new("RGBA", (step * n, target_h), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f, (i * step, 0))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return uri, fw, n, gif.info.get("duration", 100)


class GifTrack:
    """gif 帧序列 + 按累计时间轴取帧(优化过的 gif 帧时长可变,不能按固定步长索引)。"""

    def __init__(self, path: Path, height: int | None = None) -> None:
        gif = Image.open(path)
        self.frames: list[Image.Image] = []
        self.starts: list[int] = []
        t = 0
        for i in range(gif.n_frames):
            gif.seek(i)
            f = gif.convert("RGBA")
            if height is not None and f.height != height:
                f = f.resize((round(height * f.width / f.height), height), Image.BILINEAR)
            self.frames.append(f)
            self.starts.append(t)
            t += gif.info.get("duration", 100)
        self.total = t

    def at(self, t: int) -> Image.Image:
        t %= self.total
        return self.frames[bisect.bisect_right(self.starts, t) - 1]


def main() -> None:
    cat_track = GifTrack(ROOT / "zabanwalk.gif", height=100 * SCALE)
    # 聊天头像:雪碧图按设备像素(34*SCALE)制作,CSS 尺寸减回 34px,
    # 在 2x 屏上 1:1 显示不发糊
    av_uri, av_fw, n, walk_ms = walk_sprite(34 * SCALE)
    av_step_css = (av_fw + SPRITE_GUTTER) // SCALE

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(PORT)], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("localhost", PORT), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        frames_rgb: list[Image.Image] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1456, "height": 1000},
                                    device_scale_factor=SCALE,
                                    reduced_motion="reduce")  # 网站动画脚本退出,改由本脚本逐帧驱动
            page.goto(URL, wait_until="networkidle")
            # 走路猫录屏时隐藏(占位不变),由 PIL 后期贴原 gif 帧;
            # 聊天卡是顺应页面配色特调的,保留网页实录,bot 头像换雪碧图驱动
            page.add_style_tag(content=f"""
              .hero-right > img {{ visibility: hidden; }}
              .chat-msg {{ transition: none !important; }}
              .chat-caret {{ display: inline-block !important; animation: none !important; }}
              .chat-avatar-bot img {{ display: none !important; }}
              .chat-avatar-bot::before {{
                content: ''; display: block; width: {av_fw // SCALE}px; height: 34px;
                background-image: url({av_uri});
                background-size: {(av_fw + SPRITE_GUTTER) * n // SCALE}px 34px;
                background-position: var(--walk-av, 0px) 0;
              }}
            """)
            page.evaluate("document.fonts.ready")
            # 预创建蹦猫 span(关掉 CSS 动画,逐帧手动驱动)
            pops = make_pops()
            page.evaluate(
                """(pops) => {
                  const bs = document.querySelector('.hero-btns').children;
                  pops.forEach((p, i) => {
                    const s = document.createElement('span');
                    s.className = 'cat-pop';
                    s.id = 'pop-' + i;
                    s.textContent = p.face;
                    s.style.left = p.left + '%';
                    s.style.animation = 'none';
                    s.style.opacity = '0';
                    bs[p.btn].appendChild(s);
                  });
                }""",
                pops,
            )
            set_state = (SET_STATE_JS
                         .replace("__POPS__", json.dumps(pops, ensure_ascii=False))
                         .replace("WALK_FRAME_MS", str(walk_ms))
                         .replace("WALK_N", str(n))
                         .replace("AV_STEP", str(av_step_css)))
            # 裁掉左右留白:横向取 hero-inner(内容区),纵向取 hero 全高
            hero_box = page.locator(".hero").bounding_box()
            inner_box = page.locator(".hero-inner").bounding_box()
            pad = 28
            clip = {
                "x": inner_box["x"] - pad,
                "y": hero_box["y"],
                "width": inner_box["width"] + pad * 2,
                "height": hero_box["height"],
            }
            # 大猫贴图位置(CSS px,相对裁剪区,居中对齐原 <img> 框)
            cat_rect = page.evaluate(
                "() => { const r = document.querySelector('.hero-right > img')"
                ".getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; }")
            page.screenshot(clip=clip)  # 预热渲染管线

            for t in range(0, LOOP_MS, FRAME_MS):
                page.evaluate(set_state, t)
                # 等新样式真正上屏再截:立刻截会截到 compositor 画了一半的混合帧
                # (= gif 里的"重影")。headless 下 rAF 不会自行触发,必须真实等待
                page.wait_for_timeout(80)
                png = page.screenshot(clip=clip)
                frames_rgb.append(Image.open(io.BytesIO(png)).convert("RGB"))
            browser.close()
    finally:
        server.terminate()

    # 后期贴走路猫:PIL 直接合成 zabanwalk 原帧,逐像素确定
    for idx, frame in enumerate(frames_rgb):
        pose = cat_track.at(idx * FRAME_MS)
        px = round((cat_rect[0] - clip["x"]) * SCALE + (cat_rect[2] * SCALE - pose.width) / 2)
        py = round((cat_rect[1] - clip["y"]) * SCALE + (cat_rect[3] * SCALE - pose.height) / 2)
        frame.paste(pose, (px, py), pose)

    # 全局调色板 + RGB 帧间差分。调色板不能只取首帧:消息/徽章(如 Discord
    # blurple 的 ✓APP)是后出场的,首帧没有这些颜色会被映射偏色。
    # 取首帧+尾帧(消息全部显示)纵向拼接共同生成。
    # 关抖动:页面噪点纹理本身就是天然抖动,FS 抖动反而让边缘像素逐帧闪动
    w0, h0 = frames_rgb[0].size
    pal_src = Image.new("RGB", (w0, h0 * 2))
    pal_src.paste(frames_rgb[0], (0, 0))
    pal_src.paste(frames_rgb[-1], (0, h0))
    pal0 = pal_src.quantize(colors=255, method=Image.FASTOCTREE, dither=Image.Dither.NONE)
    quant = [f.quantize(palette=pal0, dither=Image.Dither.NONE) for f in frames_rgb]
    rgbs = [np.asarray(f) for f in frames_rgb]
    out_frames = [quant[0]]
    for i in range(1, len(quant)):
        changed = (rgbs[i] != rgbs[i - 1]).any(axis=2)
        a = np.asarray(quant[i]).copy()
        a[~changed] = 255  # 未变像素 → 透明索引,叠在上一帧上(disposal=1)
        diff = quant[i].copy()
        diff.putdata(a.flatten())
        diff.info["transparency"] = 255
        out_frames.append(diff)

    if OUT.exists() and not OUT.with_suffix(".gif.bak").exists():
        shutil.copy(OUT, OUT.with_suffix(".gif.bak"))
    out_frames[0].save(
        # optimize 会重排每帧调色板,部分查看器对透明索引重映射处理不一致 → 残影
        OUT, save_all=True, append_images=out_frames[1:], duration=FRAME_MS,
        loop=0, disposal=1, transparency=255, optimize=False,
    )
    w, h = frames_rgb[0].size
    print(f"{OUT.name}: {len(out_frames)} frames @ {1000 // FRAME_MS}fps, "
          f"{OUT.stat().st_size / 1024:.0f} KB, {w}x{h}")


if __name__ == "__main__":
    main()
