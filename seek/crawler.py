"""
微信公众号文章爬取模块

爬取流程（基于 we-mp-rss 的方法）：
1. 通过微信公众平台 API (cgi-bin/appmsg) 获取文章列表
2. 通过 Playwright 打开文章链接获取正文内容
3. 根据发布时间筛选文章
"""
import asyncio
import os
import re
import sys
import time
import random
from typing import Optional

import aiohttp
import requests

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from .models import Article, Subscription, TokenStore
from .auth import WeChatAuth


class WeChatCrawler:
    """微信公众号文章爬取器"""

    # 文章列表 API
    APPMSG_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"
    # 文章发布列表 API
    PUBLISH_URL = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish"

    USER_AGENTS = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1",
    ]

    def __init__(self, token_store: Optional[TokenStore] = None):
        self.token_store = token_store or TokenStore()
        self.auth = WeChatAuth(self.token_store)
        self.session = requests.Session()
        self.session.verify = False
        # 禁用 SSL 警告
        import urllib3
        urllib3.disable_warnings()

    # ------------------------------------------------------------------
    # 文章列表爬取 (API 方式)
    # ------------------------------------------------------------------

    def fetch_article_list(
        self,
        sub: Subscription,
        max_pages: int = 2,
        interval: int = 10,
    ) -> list[Article]:
        """
        通过微信公众平台 API 获取文章列表

        Args:
            sub: 订阅的公众号
            max_pages: 最大爬取页数（每页约5条）
            interval: 请求间隔秒数

        Returns:
            Article 列表（不含正文内容）
        """
        articles: list[Article] = []
        token = self.token_store.get_token()
        cookie = self.token_store.get_cookie()
        fakeid = sub.fakeid

        if not token or not cookie:
            print("错误: 未登录微信公众平台，请先运行登录")
            return articles
        if not fakeid:
            print(f"错误: 公众号 [{sub.name}] 缺少 fakeid，请确认订阅信息完整")
            return articles

        count = 5
        headers = self._build_headers(cookie)

        for page in range(max_pages):
            begin = page * count
            params = {
                "action": "list_ex",
                "begin": begin,
                "count": count,
                "fakeid": fakeid,
                "type": "9",
                "token": token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            }

            try:
                time.sleep(random.randint(0, interval))
                resp = self.session.get(
                    self.APPMSG_URL,
                    params=params,
                    headers=headers,
                    timeout=(10, 30),
                )
                msg = resp.json()

                ret = msg.get("base_resp", {}).get("ret", -1)
                if ret == 200013:
                    print(f"频率限制，停止于第{page+1}页")
                    break
                if ret == 200003:
                    print(f"Session失效，请重新登录")
                    break
                if ret != 0:
                    err_msg = msg.get("base_resp", {}).get("err_msg", "未知错误")
                    print(f"API错误: {err_msg} (code={ret})")
                    break

                app_msg_list = msg.get("app_msg_list", [])
                if not app_msg_list:
                    break

                for item in app_msg_list:
                    article = Article(
                        aid=str(item.get("aid", "")),
                        title=item.get("title", ""),
                        url=item.get("link", ""),
                        description=item.get("digest", ""),
                        cover_url=item.get("cover", ""),
                        publish_time=int(item.get("update_time", 0)),
                        mp_name=sub.name,
                        mp_id=sub.mp_id,
                        biz=sub.biz,
                    )
                    articles.append(article)

                print(f"  第{page+1}页: 获取 {len(app_msg_list)} 篇")

                if len(app_msg_list) < count:
                    break

            except requests.exceptions.Timeout:
                print(f"  第{page+1}页请求超时")
                break
            except Exception as e:
                print(f"  第{page+1}页请求异常: {e}")
                break

        print(f"共获取 {len(articles)} 篇文章信息（不含内容）")
        return articles

    # ------------------------------------------------------------------
    # 文章内容抓取 (Playwright 方式)
    # ------------------------------------------------------------------

    async def fetch_article_content(
        self, article: Article
    ) -> Optional[str]:
        """
        使用 Playwright 浏览器获取文章正文内容和图片（异步）

        Args:
            article: 文章对象（需含 url）

        Returns:
            文章正文纯文本，失败返回 None
        """
        from playwright.async_api import async_playwright
        from bs4 import BeautifulSoup

        try:
            async with async_playwright() as p:
                browser = await p.webkit.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 414, "height": 896},
                    user_agent=random.choice(self.USER_AGENTS),
                )
                page = await context.new_page()

                try:
                    await page.goto(
                        article.url, wait_until="domcontentloaded", timeout=30000
                    )
                    await asyncio.sleep(2)

                    body_text = await page.locator("body").text_content()

                    # 检查异常状态
                    if "当前环境异常" in (body_text or ""):
                        print(f"  环境异常，无法访问: {article.title[:30]}")
                        return None
                    if "该内容已被发布者删除" in (body_text or ""):
                        print(f"  文章已删除: {article.title[:30]}")
                        return None
                    if "内容审核中" in (body_text or ""):
                        print(f"  文章审核中: {article.title[:30]}")
                        return None

                    # 提取正文 HTML
                    content_html = ""
                    for selector in ["#js_content", "#js_article", ".rich_media_content"]:
                        try:
                            content_html = await page.locator(selector).inner_html()
                            if content_html:
                                break
                        except Exception:
                            continue

                    # 提取图片 URL（暂存，筛选后再下载）
                    if content_html:
                        soup = BeautifulSoup(content_html, "html.parser")
                        img_urls = []
                        for img in soup.find_all("img"):
                            src = img.get("data-src") or img.get("src") or ""
                            if src and "mmbiz.qpic.cn" in src:
                                img_urls.append(src)
                        article._img_urls = img_urls

                    # 转换为纯文本
                    if content_html:
                        text = soup.get_text(separator="\n", strip=True)
                        text = re.sub(r"\n{3,}", "\n\n", text)
                        return text

                    return body_text or ""

                finally:
                    await browser.close()

        except Exception as e:
            print(f"  获取文章内容失败 [{article.title[:30]}]: {e}")
            return None

    async def download_images(
        self, article: Article, img_urls: list[str] = None
    ) -> None:
        """下载文章图片到 Blog/{article_dir}/ 目录"""
        if img_urls is None:
            img_urls = article._img_urls
        if not img_urls:
            return

        if article.article_dir:
            img_dir = os.path.join("Blog", article.article_dir)
        else:
            aid = article.aid or re.sub(r"[^\w\-]", "_", article.url)[-20:]
            img_dir = os.path.join("Blog", aid)
        os.makedirs(img_dir, exist_ok=True)

        connector = aiohttp.TCPConnector(verify_ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            for i, url in enumerate(img_urls):
                try:
                    headers = {
                        "Referer": article.url,
                        "User-Agent": random.choice(self.USER_AGENTS),
                    }
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            ext = ".jpg"
                            content_type = resp.headers.get("Content-Type", "")
                            if "png" in content_type:
                                ext = ".png"
                            elif "gif" in content_type:
                                ext = ".gif"
                            elif "webp" in content_type:
                                ext = ".webp"
                            filename = f"{i+1:02d}{ext}"
                            filepath = os.path.join(img_dir, filename)
                            with open(filepath, "wb") as f:
                                f.write(await resp.read())
                            article.images.append(filepath)
                except Exception as e:
                    print(f"  下载图片失败 [{i+1}/{len(img_urls)}]: {e}")

        if article.images:
            print(f"  下载 {len(article.images)}/{len(img_urls)} 张图片")

    async def fetch_contents_batch(
        self, articles: list[Article], max_concurrent: int = 3
    ) -> None:
        """批量获取文章内容（控制并发数）"""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_one(article: Article):
            async with semaphore:
                content = await self.fetch_article_content(article)
                if content:
                    article.content_text = content
                # 请求间隔
                await asyncio.sleep(random.uniform(1, 3))

        tasks = [fetch_one(a) for a in articles]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_headers(self, cookie: str) -> dict:
        return {
            "Cookie": cookie,
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://mp.weixin.qq.com/",
        }

    @staticmethod
    def filter_by_time_window(
        articles: list[Article],
        start_time: int,
        end_time: int,
    ) -> list[Article]:
        """
        按发布时间窗口筛选文章

        Args:
            articles: 文章列表
            start_time: 窗口起始时间戳
            end_time: 窗口结束时间戳

        Returns:
            符合时间窗口的文章列表
        """
        filtered = [
            a
            for a in articles
            if start_time <= a.publish_time <= end_time
        ]
        return filtered
