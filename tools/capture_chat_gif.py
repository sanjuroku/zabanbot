# 抓取主页 hero 对话卡片，导出三语透明底 GIF（逐帧显式设置状态，25fps）
# 用法：python tools/capture_chat_gif.py  （需 http://localhost:8800 在跑）
#
# 不依赖浏览器时钟：每一帧的打字进度 / 消息淡入 / 光标亮灭 / 走路帧
# 都按时间 t 由脚本直接写进 DOM，节奏与网站 JS 时间线严格一致。
import base64
import io
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

URL = "http://localhost:8800"
OUT = Path(__file__).resolve().parent.parent / "screenshots"
LOOP_MS = 6900          # 网站动画周期
FRAME_MS = 40           # 25fps
LANGS = ["zh", "en", "ja"]

def quantize_frame(img: Image.Image) -> Image.Image:
    """RGBA -> 带 1bit 透明的调色板帧"""
    alpha = img.getchannel("A")
    p = img.convert("RGB").quantize(colors=255, method=Image.FASTOCTREE)
    mask = alpha.point(lambda a: 255 if a < 128 else 0)
    p.paste(255, mask)  # 索引 255 = 透明
    p.info["transparency"] = 255
    return p

def walk_sprite() -> tuple[str, float, int, int]:
    """zabanwalk.gif -> 雪碧图 data URI。返回 (uri, 单帧CSS宽, 帧数, 单帧时长ms)"""
    gif = Image.open(Path(__file__).resolve().parent.parent / "zabanwalk.gif")
    frames = []
    for i in range(gif.n_frames):
        gif.seek(i)
        frames.append(gif.convert("RGBA"))
    n, (w, h) = len(frames), frames[0].size
    frame_ms = gif.info.get("duration", 100)
    sheet = Image.new("RGBA", (w * n, h), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f, (i * w, 0))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return uri, 34 * w / h, n, frame_ms

# 与 index.html chatAnim 的时间线一致；缓动用 quadOut 近似 CSS ease-out
SET_STATE_JS = """
(t) => {
  const mom   = document.getElementById('chat-msg-mom');
  const bot   = document.getElementById('chat-msg-bot');
  const typed = document.getElementById('chat-typed');
  const caret = document.getElementById('chat-caret');
  const ph    = document.getElementById('chat-placeholder');
  // 打字与占位符：2100 打「咋」，2450 打「咋办」，3150 发送清空
  let txt = '', phVis = true;
  if (t >= 2100 && t < 2450) { txt = '咋'; phVis = false; }
  else if (t >= 2450 && t < 3150) { txt = '咋办'; phVis = false; }
  typed.textContent = txt;
  ph.style.display = phVis ? '' : 'none';
  // 消息淡入：250ms，opacity 0->1 / translateY 4px->0
  const fade = (el, t0) => {
    const p = Math.min(Math.max((t - t0) / 250, 0), 1);
    const e = 1 - Math.pow(1 - p, 2);
    el.style.opacity = t < t0 ? '0' : String(e);
    el.style.transform = `translateY(${(4 * (1 - e)).toFixed(2)}px)`;
  };
  fade(mom, 3150);
  fade(bot, 3900);
  // 光标：1.06s 周期 steps(1)，前 49% 亮（与网站 caret-blink 一致）
  caret.style.opacity = (t % 1060) < 520 ? '1' : '0';
  // 走路帧
  const fi = Math.floor(t / WALK_FRAME_MS) % WALK_N;
  document.documentElement.style.setProperty('--walk-x', `${-(fi * WALK_FW).toFixed(2)}px`);
}
"""

def capture_lang(browser, lang: str, sprite):
    uri, fw, n, frame_ms = sprite
    page = browser.new_page(viewport={"width": 1280, "height": 900},
                            device_scale_factor=2,
                            reduced_motion="reduce")  # 让页面自身的动画脚本退出
    page.goto(URL, wait_until="networkidle")
    page.add_style_tag(content=f"""
      html, body, .hero, .hero-inner, .hero-right {{ background: transparent !important; }}
      /* GIF 专用 Discord 深色配色（官网保持金棕主题不受影响） */
      .hero-chat {{
        box-shadow: none !important;
        background: #070709 !important;
        border-color: rgba(255,255,255,0.08) !important;
      }}
      .chat-input {{ background: #131416 !important; }}
      .chat-msg {{ transition: none !important; }}
      .chat-caret {{ display: inline-block !important; animation: none !important; }}
      .chat-avatar-bot img {{ display: none !important; }}
      .chat-avatar-bot::before {{
        content: ''; display: block; width: 34px; height: 34px;
        background-image: url({uri});
        background-size: {fw * n:.2f}px 34px;
        background-position: var(--walk-x, 0px) 0;
      }}
    """)
    page.evaluate("document.fonts.ready")
    page.evaluate(f"applyLang('{lang}')")
    set_state = SET_STATE_JS.replace("WALK_FRAME_MS", str(frame_ms)) \
                            .replace("WALK_N", str(n)) \
                            .replace("WALK_FW", f"{fw:.4f}")
    card = page.locator(".hero-chat")
    card.screenshot(omit_background=True)  # 预热渲染管线

    frames = []
    for t in range(0, LOOP_MS, FRAME_MS):
        page.evaluate(set_state, t)
        png = card.screenshot(omit_background=True)
        frames.append(Image.open(io.BytesIO(png)).convert("RGBA"))

    pal = [quantize_frame(f) for f in frames]
    out = OUT / f"hero-chat-{lang}.gif"
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=FRAME_MS,
                loop=0, disposal=2, transparency=255, optimize=False)
    print(f"{out.name}: {len(frames)} captured frames @25fps, "
          f"{out.stat().st_size/1024:.0f} KB, size {frames[0].size}")
    page.close()

def main():
    OUT.mkdir(exist_ok=True)
    sprite = walk_sprite()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for lang in LANGS:
            capture_lang(browser, lang, sprite)
        browser.close()

if __name__ == "__main__":
    main()
