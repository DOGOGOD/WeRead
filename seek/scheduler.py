"""
调度器模块

基于 APScheduler 实现定时任务调度。
每次执行：爬取文章列表 → 按时间窗口筛选 → 爬取正文 → 关键词粗筛 →
输出结构化 Markdown，由 OpenClaw AI 完成最终摘要。
"""
import asyncio
import os
import re
import shutil
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .models import Subscription, TokenStore
from .crawler import WeChatCrawler


class WeReadScheduler:
    """定时任务调度器 — 爬取 + 输出结构化数据，AI 总结由 OpenClaw 完成"""

    WINDOW_MAP = {
        10: (-12, 0, "前一日22:00 - 今日10:00"),
        14: (-4, 0, "今日10:00 - 14:00"),
        18: (-4, 0, "今日14:00 - 18:00"),
        22: (-4, 0, "今日18:00 - 22:00"),
    }

    def __init__(
        self,
        subscriptions: list[Subscription],
        token_store: Optional[TokenStore] = None,
        max_pages: int = 2,
        request_interval: int = 10,
        output_dir: str = "./Blog",
    ):
        self.subscriptions = subscriptions
        self.token_store = token_store or TokenStore()
        self.crawler = WeChatCrawler(self.token_store)
        self.max_pages = max_pages
        self.request_interval = request_interval
        self.output_dir = output_dir
        self.scheduler = BackgroundScheduler()

    def get_time_window(self, now: Optional[datetime] = None) -> tuple[int, int, str]:
        now = now or datetime.now()
        hour = now.hour

        window_config = self.WINDOW_MAP.get(hour)
        if window_config is None:
            start_offset, end_offset = -4, 0
            desc = f"{(now - timedelta(hours=4)).strftime('%H:%M')} - {now.strftime('%H:%M')}"
        else:
            start_offset, end_offset, desc = window_config

        start_time = now + timedelta(hours=start_offset)
        end_time = now + timedelta(hours=end_offset)
        return int(start_time.timestamp()), int(end_time.timestamp()), desc

    def run_once(self) -> Optional[str]:
        start_ts, end_ts, window_desc = self.get_time_window()
        start_str = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M")
        end_str = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        print(f"\n{'='*60}")
        print(f"WeRead 执行 @ {now_str}")
        print(f"时间窗口: {start_str} ~ {end_str} ({window_desc})")
        print(f"{'='*60}\n")

        all_articles = []
        for sub in self.subscriptions:
            articles = asyncio.run(
                self._process_subscription(sub, start_ts, end_ts)
            )
            all_articles.extend(articles)

        if not all_articles:
            print("\n本期无新文章。")
            return None

        filepath = self._write_index_markdown(all_articles, window_desc)
        print(f"\n文章数据已保存至: {os.path.abspath(filepath)}")
        self._cleanup_blog_dirs()
        return filepath

    def _cleanup_blog_dirs(self, max_dirs: int = 100, remove_count: int = 50) -> None:
        if not os.path.isdir(self.output_dir):
            return

        dirs = []
        for name in os.listdir(self.output_dir):
            path = os.path.join(self.output_dir, name)
            if os.path.isdir(path):
                date_str = name[:8] if len(name) >= 8 else ""
                dirs.append((date_str, path))

        if len(dirs) <= max_dirs:
            return

        dirs.sort(key=lambda x: x[0])
        for _, path in dirs[:remove_count]:
            try:
                shutil.rmtree(path)
                print(f"  清理旧目录: {os.path.basename(path)}")
            except Exception as e:
                print(f"  清理失败 [{os.path.basename(path)}]: {e}")

        print(f"Blog 清理完成: 删除 {min(remove_count, len(dirs))} 个旧目录，剩余 {len(dirs) - remove_count} 个")

    async def _process_subscription(
        self, sub: Subscription, start_ts: int, end_ts: int
    ) -> list[dict]:
        print(f"\n--- {sub.name} ---")

        # 1. 爬取文章列表
        print("  [1/4] 爬取文章列表...")
        articles = self.crawler.fetch_article_list(
            sub, max_pages=self.max_pages, interval=self.request_interval
        )
        if not articles:
            print("  未获取到文章")
            return []

        # 2. 按时间窗口筛选
        print(f"  [2/4] 时间窗口筛选 (共 {len(articles)} 篇)...")
        filtered = self.crawler.filter_by_time_window(articles, start_ts, end_ts)
        print(f"  窗口内: {len(filtered)} 篇")
        if not filtered:
            return []

        # 生成 Blog 目录名
        for a in filtered:
            date_str = datetime.fromtimestamp(a.publish_time).strftime("%Y%m%d")
            safe_title = re.sub(r'[^\w一-鿿]', '_', a.title)[:30].strip('_')
            a.article_dir = f"{date_str}_{safe_title}_{a.aid[-6:]}"

        # 3. 爬取正文
        print("  [3/4] 爬取文章正文和图片...")
        await self.crawler.fetch_contents_batch(filtered)
        content_count = sum(1 for a in filtered if a.content_text)
        print(f"  成功获取 {content_count}/{len(filtered)} 篇正文")

        # 4. 关键词粗筛 → 下载图片 → 写入 Blog
        print("  [4/4] 关键词筛选，下载图片并写入 Blog...")
        result = []
        for article in filtered:
            if not article.content_text:
                continue

            if sub.keywords:
                text = f"{article.title} {article.description} {article.content_text[:1000]}".lower()
                matched = [kw for kw in sub.keywords if kw.lower() in text]
                if not matched:
                    continue
                match_info = f"命中: {', '.join(matched)}"
            else:
                match_info = ""

            blog_dir = os.path.join(self.output_dir, article.article_dir)
            os.makedirs(blog_dir, exist_ok=True)
            if article._img_urls:
                await self.crawler.download_images(article)
                if article.images:
                    print(f"    下载 {len(article.images)}/{len(article._img_urls)} 张图片 -> {article.article_dir}")

            self._write_article_file(article)

            result.append({
                "title": article.title,
                "url": article.url,
                "description": article.description,
                "content": article.content_text,
                "images": article.images,
                "article_dir": article.article_dir,
                "publish_time": article.publish_time,
                "publish_time_str": datetime.fromtimestamp(article.publish_time).strftime("%Y-%m-%d %H:%M"),
                "mp_name": article.mp_name,
                "match_info": match_info,
                "interest": sub.interest,
            })

        print(f"  关键词筛选后: {len(result)} 篇")
        return result

    def _write_article_file(self, article) -> None:
        blog_dir = os.path.join(self.output_dir, article.article_dir)
        os.makedirs(blog_dir, exist_ok=True)

        lines = [
            f"# {article.title}",
            "",
            f"- **公众号**: {article.mp_name}",
            f"- **发布时间**: {datetime.fromtimestamp(article.publish_time).strftime('%Y-%m-%d %H:%M')}",
            f"- **原文链接**: {article.url}",
            f"- **原文摘要**: {article.description or '(无)'}",
            "",
            "---",
            "",
            article.content_text,
            "",
        ]

        if article.images:
            lines.extend([
                "---", "",
                f"## 正文图片 ({len(article.images)} 张)", "",
            ])
            for img_path in article.images:
                img_name = os.path.basename(img_path)
                lines.append(f"![{img_name}]({img_name})")
            lines.append("")

        filepath = os.path.join(blog_dir, "article.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def _write_index_markdown(
        self, articles: list[dict], window_desc: str
    ) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        filepath = os.path.join(self.output_dir, f"index_{timestamp}.md")

        by_mp: dict[str, list[dict]] = {}
        for a in articles:
            mp = a.get("mp_name", "未知")
            by_mp.setdefault(mp, []).append(a)

        lines = [
            "# WeRead 文章抓取结果",
            "",
            f"**抓取时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"**时间窗口**: {window_desc}",
            f"**文章总数**: {len(articles)} 篇",
            "",
            "---",
            "",
            "## 你的任务",
            "",
            "每篇文章的正文和图片已保存在各自的 Blog 目录下（见下方链接）。",
            "请根据以下信息，为用户生成一份**条理清晰的阅读简报**：",
            "",
            "1. 先用每个公众号的「用户关注点」判断文章是否真正相关，过滤掉无关内容",
            "2. 对每篇相关文章，点击目录链接进入，用 Read 工具读取 article.md 正文",
            "3. 摘要需包含：核心主题、关键观点/数据/人物、与用户关注点的关联",
            "4. 如果文章包含图片，用 Read 工具读取图片，理解其中内容",
            "5. 根据用户关注点筛选出最相关的 1-3 张图片，在简报中展示并附简短说明",
            "6. 最后生成一份综合简报，按公众号分组呈现",
            '7. 如果某公众号本期无相关文章，直接说"本期无相关内容"',
            "",
            "---",
            "",
        ]

        for mp_name, mp_articles in by_mp.items():
            interest = mp_articles[0].get("interest", "") if mp_articles else ""
            lines.append(f"## {mp_name}")
            lines.append("")
            if interest:
                lines.append(f"**用户关注点**: {interest}")
                lines.append("")
            lines.append(f"共 {len(mp_articles)} 篇文章（已通过关键词粗筛）：")
            lines.append("")

            for i, a in enumerate(mp_articles, 1):
                article_dir = a.get("article_dir", "")
                blog_path = f"{self.output_dir}/{article_dir}"
                lines.append(f"### 文章 {i}: {a['title']}")
                lines.append("")
                lines.append(f"- **发布时间**: {a['publish_time_str']}")
                if a.get("match_info"):
                    lines.append(f"- **关键词匹配**: {a['match_info']}")
                lines.append(f"- **原文摘要**: {a.get('description', '(无)')}")
                lines.append(f"- **原文链接**: {a['url']}")
                lines.append(f"- **本地目录**: `{blog_path}/`")
                lines.append(f"  - 正文: `{blog_path}/article.md`")
                if a.get("images"):
                    lines.append(f"  - 图片: {len(a['images'])} 张")
                lines.append("")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return filepath

    def start(self, cron_expressions: list[str] = None) -> None:
        if cron_expressions is None:
            cron_expressions = [
                "0 10 * * *",
                "0 14 * * *",
                "0 18 * * *",
                "0 22 * * *",
            ]

        for i, cron_expr in enumerate(cron_expressions):
            self.scheduler.add_job(
                self.run_once,
                CronTrigger.from_crontab(cron_expr),
                id=f"weread_{i}",
                name=f"WeRead-{cron_expr}",
                replace_existing=True,
            )
            print(f"已添加定时任务: {cron_expr}")

        self.scheduler.start()
        print("定时调度器已启动")
        print(f"当前订阅: {len(self.subscriptions)} 个公众号")
        print("按 Ctrl+C 退出\n")

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            print("调度器已停止")
