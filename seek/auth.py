"""
微信公众平台认证模块

支持两步登录流程（适配 OpenClaw 等 Agent 环境）:
  1. generate_qrcode() → 获取二维码图片，发送给用户
  2. wait_for_scan()   → 等待用户扫码，提取 token/cookie
"""
import os
import sys
import asyncio
import re
from typing import Optional

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from .models import TokenStore

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
VIEWPORT = {"width": 1280, "height": 900}
QR_SELECTOR = ".login__type__container__scan__qrcode"


def _run_async(coro):
    """在同步上下文中运行异步协程"""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


class WeChatAuth:
    """微信公众平台认证器"""

    WX_LOGIN = "https://mp.weixin.qq.com/"
    WX_HOME = "https://mp.weixin.qq.com/cgi-bin/home"
    QRCODE_PATH = "data/wx_qrcode.png"

    def __init__(self, token_store: Optional[TokenStore] = None):
        self.token_store = token_store or TokenStore()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    def is_authenticated(self) -> bool:
        token = self.token_store.get_token()
        cookie = self.token_store.get_cookie()
        return bool(token and cookie)

    # ------------------------------------------------------------------
    # 浏览器生命周期
    # ------------------------------------------------------------------

    async def _launch_browser(self, headless: bool = True):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.webkit.launch(headless=headless)
        self._context = await self._browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
        )
        self._page = await self._context.new_page()

    async def _close_browser(self):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        finally:
            self._pw = None
            self._browser = None
            self._context = None
            self._page = None

    # ------------------------------------------------------------------
    # 二维码生成
    # ------------------------------------------------------------------

    async def _capture_qrcode(self) -> Optional[str]:
        await self._page.goto(self.WX_LOGIN, wait_until="networkidle")
        qrcode_el = await self._page.query_selector(QR_SELECTOR)
        if not qrcode_el:
            print("错误: 无法找到二维码元素")
            return None
        os.makedirs(os.path.dirname(self.QRCODE_PATH), exist_ok=True)
        await qrcode_el.screenshot(path=self.QRCODE_PATH)
        path = os.path.abspath(self.QRCODE_PATH)
        print(f"二维码已生成: {path}")
        return path

    # ------------------------------------------------------------------
    # 两步登录: 步骤1 — 生成二维码（浏览器保持打开）
    # ------------------------------------------------------------------

    def generate_qrcode(self) -> Optional[str]:
        return _run_async(self._generate_qrcode_async())

    async def _generate_qrcode_async(self) -> Optional[str]:
        print("正在启动浏览器...")
        await self._launch_browser()
        result = await self._capture_qrcode()
        if not result:
            await self._close_browser()
        return result

    # ------------------------------------------------------------------
    # 两步登录: 步骤2 — 等待扫码
    # ------------------------------------------------------------------

    def wait_for_scan(self, timeout_seconds: int = 300) -> bool:
        return _run_async(self._wait_for_scan_async(timeout_seconds))

    async def _wait_for_scan_async(self, timeout_seconds: int) -> bool:
        if not self._page:
            print("错误: 未生成二维码，请先调用 generate_qrcode")
            return False
        return await self._wait_for_login(timeout_seconds)

    async def _wait_for_login(self, timeout_seconds: int) -> bool:
        try:
            print(f"等待扫码中（最长 {timeout_seconds // 60} 分钟）...")
            await self._page.wait_for_url(
                lambda url: self.WX_HOME in url,
                timeout=timeout_seconds * 1000,
            )
            print("检测到登录跳转...")
            await self._save_auth_data()
            return True
        except Exception as e:
            if "Timeout" in str(e):
                print(f"\n扫码超时（{timeout_seconds // 60} 分钟）")
            else:
                print(f"登录出错: {e}")
            return False
        finally:
            await self._close_browser()

    # ------------------------------------------------------------------
    # 一步登录（生成二维码 + 等待扫码）
    # ------------------------------------------------------------------

    def login_with_qrcode_and_wait(self, timeout_seconds: int = 300) -> bool:
        return _run_async(self._login_one_step(timeout_seconds))

    async def _login_one_step(self, timeout_seconds: int) -> bool:
        await self._launch_browser()
        path = await self._capture_qrcode()
        if not path:
            await self._close_browser()
            return False
        print(f"QRCODE_PATH:{path}")
        return await self._wait_for_login(timeout_seconds)

    # ------------------------------------------------------------------
    # 有头登录（本地 GUI 环境）
    # ------------------------------------------------------------------

    def login_with_qrcode(self) -> bool:
        return _run_async(self._login_headful())

    async def _login_headful(self) -> bool:
        await self._launch_browser(headless=False)
        try:
            path = await self._capture_qrcode()
            if not path:
                return False
            print(f"二维码: {path}")
            print("请用微信扫码，等待中...")
            await self._page.wait_for_event("framenavigated", timeout=5 * 60 * 1000)
            if self.WX_HOME not in self._page.url:
                return False
            await self._save_auth_data()
            return True
        finally:
            await self._close_browser()

    # ------------------------------------------------------------------
    # Token 提取 & 持久化
    # ------------------------------------------------------------------

    async def _extract_token(self, page) -> str:
        current_url = page.url
        token_match = re.search(r"token=([^&]+)", current_url)
        if token_match:
            return token_match.group(1)
        for source in [
            "() => localStorage.getItem('token')",
            (
                "() => {"
                "  for (const s of document.querySelectorAll('script')) {"
                "    const m = s.textContent.match(/token\\s*=\\s*['\"]([\\d]+)['\"]/);"
                "    if (m) return m[1];"
                "  }"
                "  return '';"
                "}"
            ),
        ]:
            try:
                token = await page.evaluate(source)
                if token:
                    return token
            except Exception:
                continue
        return ""

    async def _save_auth_data(self, page=None, context=None) -> None:
        page = page or self._page
        context = context or self._context
        token = await self._extract_token(page)
        cookies = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        if not token:
            for c in cookies:
                if "token" in c["name"].lower():
                    token = c["value"]
                    break

        data = self.token_store.load()
        data["token"] = token or ""
        data["cookie"] = cookie_str
        data["cookies_list"] = [
            {
                "name": c["name"], "value": c["value"],
                "domain": c.get("domain", "mp.weixin.qq.com"),
                "path": c.get("path", "/"),
            }
            for c in cookies
        ]
        self.token_store.save(data)
        print(f"登录成功! token={token[:20] if token else '(空)'}...")

    # ------------------------------------------------------------------
    # 公众号信息提取
    # ------------------------------------------------------------------

    def extract_biz_from_url(self, url: str) -> str:
        match = re.search(r"[?&]__biz=([^&#]+)", url)
        return match.group(1) if match else ""

    def derive_fakeid_from_biz(self, biz: str) -> str:
        return biz

    async def get_fakeid_by_article_url(self, page, article_url: str) -> str:
        try:
            await page.goto(article_url, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            biz = await page.evaluate("() => window.biz || ''")
            if not biz:
                biz = self.extract_biz_from_url(article_url)
            if not biz:
                try:
                    content = await page.content()
                    match = re.search(r'var\s+biz\s*=\s*["\']([^"\']+)["\']', content)
                    if match:
                        biz = match.group(1)
                except Exception:
                    pass

            if biz:
                fakeid = self.derive_fakeid_from_biz(biz)
                data = self.token_store.load()
                fakeids = data.get("fakeids", {})
                fakeids[biz] = fakeid
                data["fakeids"] = fakeids
                self.token_store.save(data)
                return fakeid

            return ""
        except Exception as e:
            print(f"提取 fakeid 失败: {e}")
            return ""
