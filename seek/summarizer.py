"""
AI 摘要模块

支持 OpenAI 兼容接口及 Anthropic 接口。
对爬取的文章内容进行智能摘要，筛选与用户关注点相关的内容。
"""
import os
import re
from typing import Optional

import aiohttp

from .models import Article, Subscription


class AISummarizer:
    """AI 摘要生成器"""

    def __init__(
        self,
        provider: str = "openai",
        api_base: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        max_length: int = 300,
    ):
        self.provider = provider
        self.api_base = api_base.rstrip("/")
        self.api_key = self._resolve_api_key(api_key)
        self.model = model
        self.max_length = max_length

    def _resolve_api_key(self, key: str) -> str:
        """解析 API Key，支持环境变量引用 ${VAR_NAME}"""
        if key.startswith("${") and key.endswith("}"):
            env_var = key[2:-1]
            return os.environ.get(env_var, "")
        return key

    # ------------------------------------------------------------------
    # 文章相关性筛选
    # ------------------------------------------------------------------

    def filter_relevant(
        self,
        articles: list[Article],
        subscription: Subscription,
    ) -> list[Article]:
        """
        基于关键词快速筛选相关文章

        先用关键词做粗筛，避免对所有文章调用 AI。
        """
        if not subscription.keywords:
            # 没有关键词则全部保留
            return articles

        relevant = []
        for article in articles:
            text = (
                f"{article.title} {article.description} {article.content_text[:500]}".lower()
            )
            for kw in subscription.keywords:
                if kw.lower() in text:
                    relevant.append(article)
                    break
        return relevant

    async def ai_filter_relevant(
        self,
        articles: list[Article],
        subscription: Subscription,
    ) -> tuple[list[Article], list[dict]]:
        """
        使用 AI 判断文章是否与用户关注点相关

        Returns:
            (relevant_articles, filter_results):
                相关文章列表，以及每篇文章的AI判断结果
        """
        if not articles:
            return [], []

        if not self.api_key:
            print("  警告: 未配置 AI API Key，使用关键词筛选")
            return self.filter_relevant(articles, subscription), []

        interest = subscription.interest or "通用内容"
        results = []

        for article in articles:
            preview = article.content_text[:2000] if article.content_text else article.description
            if not preview or len(preview) < 50:
                results.append(
                    {"title": article.title, "relevant": False, "reason": "内容不足，跳过"}
                )
                continue

            prompt = self._build_relevance_prompt(article, interest, preview)
            try:
                response = await self._call_ai(prompt, max_tokens=150)
                result = self._parse_relevance_response(response)
                result["title"] = article.title
                results.append(result)
            except Exception as e:
                print(f"  AI 判断失败 [{article.title[:30]}]: {e}")
                results.append(
                    {"title": article.title, "relevant": False, "reason": f"判断出错: {e}"}
                )

        # 筛选出相关的文章
        relevant_aids = {
            r["title"]
            for r in results
            if r.get("relevant", False)
        }
        relevant_articles = [
            a for a in articles if a.title in relevant_aids
        ]

        return relevant_articles, results

    # ------------------------------------------------------------------
    # 文章摘要生成
    # ------------------------------------------------------------------

    async def summarize_article(
        self, article: Article, interest: str = ""
    ) -> str:
        """
        为单篇文章生成摘要

        Args:
            article: 文章对象
            interest: 用户的关注点描述

        Returns:
            文章摘要
        """
        content = article.content_text or article.description
        if not content or len(content) < 50:
            return article.description or "（内容过短，无法摘要）"

        # 截取前4000字符避免超出 token 限制
        content = content[:4000]
        prompt = self._build_summary_prompt(article, interest, content)

        try:
            summary = await self._call_ai(prompt, max_tokens=self.max_length * 2)
            return summary.strip()
        except Exception as e:
            print(f"  摘要生成失败 [{article.title[:30]}]: {e}")
            # 降级：返回原文前N字
            return content[: self.max_length] + "..."

    async def summarize_articles(
        self,
        articles: list[Article],
        subscription: Subscription,
    ) -> list[dict]:
        """
        批量生成文章摘要，并生成综合简报

        Args:
            articles: 相关文章列表
            subscription: 订阅信息

        Returns:
            摘要结果列表，每条包含 title, url, summary, relevant, reason
        """
        results = []

        for article in articles:
            summary = await self.summarize_article(
                article, subscription.interest
            )
            results.append(
                {
                    "title": article.title,
                    "url": article.url,
                    "publish_time": article.publish_time,
                    "summary": summary,
                    "mp_name": article.mp_name,
                }
            )

        return results

    async def generate_brief(
        self,
        summaries: list[dict],
        subscription_name: str,
        time_range: str,
    ) -> str:
        """生成综合简报"""
        if not summaries:
            return f"## {subscription_name}\n本期无相关内容。\n"

        articles_text = "\n\n".join(
            [
                f"### {i+1}. {s['title']}\n"
                f"- 链接: {s['url']}\n"
                f"- 摘要: {s['summary']}"
                for i, s in enumerate(summaries)
            ]
        )

        prompt = f"""你是一个专业的信息整理助手。请将以下公众号文章摘要整合成一份简洁的阅读简报。

公众号: {subscription_name}
时间范围: {time_range}
文章数量: {len(summaries)}

各文章摘要如下:
{articles_text}

请生成一份简报，格式如下:
## {subscription_name} - 阅读简报 ({time_range})

### 本时段要点
（2-3句话概括本期文章的核心内容）

### 文章列表
保留上面的文章列表格式。

要求: 简洁明了，突出关键信息，总字数不超过500字。
"""

        try:
            brief = await self._call_ai(prompt, max_tokens=800)
            return brief.strip()
        except Exception as e:
            print(f"  简报生成失败: {e}")
            # 降级：自行拼接
            return f"## {subscription_name} - 阅读简报 ({time_range})\n\n{articles_text}"

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_relevance_prompt(
        self, article: Article, interest: str, content_preview: str
    ) -> str:
        return f"""你是一个内容筛选助手。判断以下文章是否与用户的关注点相关。

用户的关注点: {interest}

文章标题: {article.title}
文章摘要: {article.description}
文章内容预览:
{content_preview}

请判断这篇文章是否与用户的关注点相关。回答格式:
相关: 是/否
理由: 一句话说明理由
"""

    def _build_summary_prompt(
        self, article: Article, interest: str, content: str
    ) -> str:
        interest_hint = ""
        if interest:
            interest_hint = (
                f"\n读者关注: {interest}\n请在摘要中侧重与读者关注点相关的内容。"
            )

        return f"""你是一个专业的内容摘要助手。请为以下微信公众号文章写一份简洁的摘要。

文章标题: {article.title}
文章描述: {article.description}
{interest_hint}
文章正文:
{content}

请写一份{self.max_length}字以内的中文摘要，要求:
1. 概括文章核心内容
2. 突出关键信息和观点
3. 语言简洁明了
4. 如果有与读者关注点相关的内容，请重点提及

摘要:
"""

    async def _call_ai(self, prompt: str, max_tokens: int = 500) -> str:
        if self.provider == "openai":
            return await self._call_openai(prompt, max_tokens)
        elif self.provider == "anthropic":
            return await self._call_anthropic(prompt, max_tokens)
        else:
            raise ValueError(f"不支持的 AI provider: {self.provider}")

    async def _call_openai(self, prompt: str, max_tokens: int) -> str:
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"API 返回 {resp.status}: {text[:200]}")
                data = await resp.json()
                return data["choices"][0]["message"]["content"]

    async def _call_anthropic(self, prompt: str, max_tokens: int) -> str:
        url = f"{self.api_base}/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"API 返回 {resp.status}: {text[:200]}")
                data = await resp.json()
                return data["content"][0]["text"]

    def _parse_relevance_response(self, response: str) -> dict:
        """解析 AI 相关性判断结果"""
        relevant = False
        reason = ""

        # 匹配 "相关: 是/否"
        relevance_match = re.search(
            r"相关[：:]\s*(是|否|yes|no)", response, re.IGNORECASE
        )
        if relevance_match:
            relevant = relevance_match.group(1).lower() in ("是", "yes")

        # 匹配 "理由: ..."
        reason_match = re.search(
            r"理由[：:]\s*(.+?)$", response, re.MULTILINE
        )
        if reason_match:
            reason = reason_match.group(1).strip()

        return {"relevant": relevant, "reason": reason}
