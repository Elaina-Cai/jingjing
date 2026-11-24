# -*- coding: utf-8 -*-
"""感知层 ① 的内容指纹（``content_sha256``，感知层.md §10-4）。

为什么需要它：跨软件同一件事（微信来一次、QQ 来一次）**算两次发生**——说者与
场合不同，所以不是去重；但事后要能把它们关联起来看，就得有一个「内容相同即
同指纹」的号码。这就是 ``content_sha256``：算正文，不算身份、不算时间、不算
会话（那三样一进哈希，「同一句话」就再也对不上了）。

为什么不算进幂等键：幂等键是 ``(平台, 账号, 会话, external_msg_id)``。把内容摘要
放进去，转发同一句话到另一个群就会互相吃掉——那是丢发生，比重复更糟。

归一化只做**逐字节可回放**的那几件事：换行统一、行尾空白去掉、首尾空白去掉。
不做全角半角、不做繁简、不做表情替换——那些一旦动手，「原文保真」就破了。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = ["normalize_body", "content_sha256", "content_sha256_of"]

_TRAILING = re.compile(r"[ \t]+$", re.MULTILINE)


def normalize_body(content: str) -> str:
    """正文归一化（只做不影响语义的空白处理）。"""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING.sub("", text)
    return text.strip()


def content_sha256(content: str) -> str:
    """正文的 sha256（64 位小写十六进制）；空正文返回空串（= 没有内容可关联）。"""
    body = normalize_body(content)
    if not body:
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()

def content_sha256_of(event: Any) -> str:
    """从事件对象取内容指纹（账本行里也存了同一份，见 ``_metadata_with_content_hash``）。

    两处都提供是因为用途不同：SQL 侧要 ``json_extract(metadata_json,'$.content_sha256')``
    做跨软件关联；Python 侧手上只有 ``Event`` 时不该去翻 ``metadata``（那是旁注，
    谁都能改），重算一遍最省心——正文归一化幂等，算一次与存一次同值。
    """
    return content_sha256(str(getattr(event, "content", "") or ""))
