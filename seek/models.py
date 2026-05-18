"""数据模型定义"""
from dataclasses import dataclass, field
from typing import Optional
import json
import os


@dataclass
class Article:
    """微信公众号文章"""
    aid: str                      # 文章唯一ID
    title: str                    # 标题
    url: str                      # 文章链接
    description: str = ""         # 摘要/描述
    content: str = ""             # 正文内容（HTML）
    content_text: str = ""        # 正文内容（纯文本）
    cover_url: str = ""           # 封面图URL
    images: list = field(default_factory=list)  # 正文图片本地路径列表
    _img_urls: list = field(default_factory=list)  # 待下载的图片URL（筛选后按需下载）
    article_dir: str = ""         # Blog 下的文章目录名
    publish_time: int = 0         # 发布时间戳
    mp_name: str = ""             # 公众号名称
    mp_id: str = ""               # 公众号ID
    biz: str = ""                 # 公众号biz标识

    def to_dict(self) -> dict:
        return {
            "aid": self.aid,
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "content_text": self.content_text[:500] if self.content_text else "",
            "cover_url": self.cover_url,
            "publish_time": self.publish_time,
            "mp_name": self.mp_name,
            "mp_id": self.mp_id,
        }


@dataclass
class Subscription:
    """订阅的公众号"""
    name: str                     # 公众号名称
    url: str                      # 用于识别的文章链接
    keywords: list = field(default_factory=list)  # 筛选关键词
    interest: str = ""            # 用户关注点描述
    fakeid: str = ""              # 微信API用到的fakeid
    biz: str = ""                 # 公众号biz标识
    mp_id: str = ""               # 公众号内部ID

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "keywords": self.keywords,
            "interest": self.interest,
            "fakeid": self.fakeid,
            "biz": self.biz,
            "mp_id": self.mp_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Subscription":
        return cls(
            name=d.get("name", ""),
            url=d.get("url", ""),
            keywords=d.get("keywords", []),
            interest=d.get("interest", ""),
            fakeid=d.get("fakeid", ""),
            biz=d.get("biz", ""),
            mp_id=d.get("mp_id", ""),
        )


class TokenStore:
    """Token/Cookie 持久化存储"""

    def __init__(self, path: str = "data/token.json"):
        self.path = path

    def load(self) -> dict:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_token(self) -> str:
        return self.load().get("token", "")

    def get_cookie(self) -> str:
        return self.load().get("cookie", "")

    def get_fakeid(self, biz: str) -> str:
        """获取指定biz对应的fakeid"""
        fakeids = self.load().get("fakeids", {})
        return fakeids.get(biz, "")
