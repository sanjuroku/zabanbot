# 抓取主页 hero 对话卡片，导出三语透明底 GIF（CDP 虚拟时间逐帧，25fps）
# 用法：python tools/capture_chat_gif.py  （需 http://localhost:8800 在跑）
import io
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

URL = "http://localhost:8800"
OUT = Path(__file__).resolve().parent.parent / "screenshots"
LOOP_MS = 6900          # 动画一个完整周期
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

class VirtualClock:
    """通过 CDP 虚拟时间精确推进页面时钟"""
    def __init__(self, page):
        self.page = page
        self.cdp = page.context.new_cdp_session(page)
        self.expired = False
        def _on_expired(_):
            self.expired = True
        self.cdp.on("Emulation.virtualTimeBudgetExpired", _on_expired)
        self.cdp.send("Emulation.setVirtualTimePolicy", {"policy": "pause"})

    def advance(self, ms: float):
        self.expired = False
        self.cdp.send("Emulation.setVirtualTimePolicy",
                      {"policy": "pauseIfNetworkFetchesPending", "budget": ms})
        # 同步 API 需主动泵事件循环，等待预算耗尽
        for _ in range(2000):
            if self.expired:
                return
            self.page.wait_for_timeout(2)
        raise TimeoutError("virtual time budget did not expire")

def capture_lang(browser, lang: str):
    page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
    page.goto(URL, wait_until="networkidle")
    page.add_style_tag(content="""
      html, body, .hero, .hero-inner, .hero-right { background: transparent !important; }
      .hero-chat { box-shadow: none !important; }
      .chat-caret { animation-duration: 1.15s !important; } /* 6 闪 / 6.9s，循环无缝 */
    """)
    page.evaluate("document.fonts.ready")
    page.evaluate(f"applyLang('{lang}')")
    card = page.locator(".hero-chat")
    card.screenshot(omit_background=True)  # 预热渲染管线

    clock = VirtualClock(page)
    mom_shown = "document.getElementById('chat-msg-mom').classList.contains('show')"

    # 锚定循环起点：推进虚拟时间，等妈妈消息出现再消失（= 新周期 t0）
    for _ in range(400):
        if page.evaluate(mom_shown):
            break
        clock.advance(50)
    for _ in range(400):
        if not page.evaluate(mom_shown):
            break
        clock.advance(50)
    # t0：重置光标闪烁相位，保证首尾衔接
    page.evaluate("""(() => {
      const c = document.getElementById('chat-caret');
      c.style.animation = 'none'; void c.offsetWidth;
      c.style.animation = 'caret-blink 1.15s steps(1) infinite';
    })()""")

    n_frames = LOOP_MS // FRAME_MS  # 172
    frames = []
    for _ in range(n_frames):
        png = card.screenshot(omit_background=True)
        frames.append(Image.open(io.BytesIO(png)).convert("RGBA"))
        clock.advance(FRAME_MS)

    pal = [quantize_frame(f) for f in frames]
    out = OUT / f"hero-chat-{lang}.gif"
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=FRAME_MS,
                loop=0, disposal=2, transparency=255, optimize=False)
    print(f"{out.name}: {len(frames)} captured frames @25fps, "
          f"{out.stat().st_size/1024:.0f} KB, size {frames[0].size}")
    page.close()

def main():
    OUT.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for lang in LANGS:
            capture_lang(browser, lang)
        browser.close()

if __name__ == "__main__":
    main()
