"""saezuri 共通データスキーマ定義

収集モジュール(search.py 等)が生成する1件分のデータを表す dataclass 群。
JSONL への保存・読み込みそのものは storage.py 側のユーティリティに委ねる想定。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class MediaItem:
    """投稿に添付されたメディア(画像・動画等)1件分"""

    type: str
    url: str


@dataclass
class Post:
    """収集した1投稿分の共通スキーマ"""

    id: str
    url: str
    author_handle: str
    author_name: str
    created_at: str
    text: str
    like_count: int
    repost_count: int
    reply_count: int
    quote_count: int
    collected_at: str
    lang: Optional[str] = None
    media: list[MediaItem] = field(default_factory=list)
    is_repost: bool = False
    quoted_id: Optional[str] = None
    source_query: Optional[str] = None

    def to_dict(self) -> dict:
        """設計書記載の項目順でJSONL保存用の辞書に変換する"""
        return {
            "id": self.id,
            "url": self.url,
            "author_handle": self.author_handle,
            "author_name": self.author_name,
            "created_at": self.created_at,
            "text": self.text,
            "lang": self.lang,
            "like_count": self.like_count,
            "repost_count": self.repost_count,
            "reply_count": self.reply_count,
            "quote_count": self.quote_count,
            "media": [asdict(m) for m in self.media],
            "is_repost": self.is_repost,
            "quoted_id": self.quoted_id,
            "source_query": self.source_query,
            "collected_at": self.collected_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        """to_dict()と同じ形の辞書からPostを復元する"""
        media = [MediaItem(**m) for m in data.get("media", [])]
        return cls(
            id=data["id"],
            url=data["url"],
            author_handle=data["author_handle"],
            author_name=data["author_name"],
            created_at=data["created_at"],
            text=data["text"],
            lang=data.get("lang"),
            like_count=data["like_count"],
            repost_count=data["repost_count"],
            reply_count=data["reply_count"],
            quote_count=data["quote_count"],
            media=media,
            is_repost=data.get("is_repost", False),
            quoted_id=data.get("quoted_id"),
            source_query=data.get("source_query"),
            collected_at=data["collected_at"],
        )


@dataclass
class TrendEntry:
    """トレンド収集1件分の共通スキーマ"""

    trend_word: str
    tweet_volume: Optional[int]
    rank: Optional[int]
    collected_at: str

    def to_dict(self) -> dict:
        """JSONL保存用の辞書に変換する"""
        return {
            "trend_word": self.trend_word,
            "tweet_volume": self.tweet_volume,
            "rank": self.rank,
            "collected_at": self.collected_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrendEntry":
        """to_dict()と同じ形の辞書からTrendEntryを復元する"""
        return cls(
            trend_word=data["trend_word"],
            tweet_volume=data.get("tweet_volume"),
            rank=data.get("rank"),
            collected_at=data["collected_at"],
        )
