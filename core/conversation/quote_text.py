# -*- coding: utf-8 -*-
"""引用内容清洗（唯一 owner）。

被引用的消息可能本身就是一条「引用/图文/媒体」消息：微信把它的 XML 塞进
``refermsg.content``，旧实现见到 ``<?xml`` 就一刀切成 ``[媒体消息]``，于是
「引用文字」在库里变成占位符（2026-10-14 实案：Elaina 引用P5的『起来嗨』
被写成 ``[媒体消息]``，鲸鲸据此答『没解析出来』）。

本函数只做确定性清洗，不读库、不调模型：
1. XML（被引用的是 appmsg）→ 解出 ``title``/``des``，剥掉它自带的
   ``[引用 …]`` 前缀，留那条消息自己的文字；只有真媒体才给占位符；
2. 非 XML 但带媒体标签 → ``[图片]``/``[表情]`` 等；
3. 纯文本 → 剥掉 ``名字:\n`` 前缀。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from safelog import warn

_MEDIA_MARKERS = (
    ("<img", "[图片]"),
    ("<emoji", "[表情]"),
    ("<videomsg", "[视频]"),
    ("<appattach", "[文件]"),
)
_QUOTE_PREFIX_RE = re.compile(r"^\[引用[^\]]*\]\s*")
# 2026-10-27: 动图/图片被当「文件」发出来时正文是 <appattach>，旧实现只认 title/des，
# 取不到就统一塌成「[文件]」—— 名字、类型、md5 全丢（实案：她引用一张 gif 问
# 「能看见吗」，鲸鲸只能答「就一个字 文件，名字内容都没过来」）。
_APPATTACH_RE = re.compile(r"<appattach>(.*?)</appattach>", re.S)
_AA_MD5_RE = re.compile(r"<md5>([0-9a-fA-F]{32})</md5>")
_AA_EXT_RE = re.compile(r"<fileext>([A-Za-z0-9]{1,8})</fileext>")
_AA_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_IMAGE_EXTS = ("gif", "png", "jpg", "jpeg", "webp", "bmp")
_NAME_PREFIX_RE = re.compile(r"^[^:\n]{1,30}:\s*\n")


def _strip_quote_prefix(text: str) -> str:
    out = text.strip()
    while True:
        new = _QUOTE_PREFIX_RE.sub("", out, count=1)
        if new == out:
            break
        out = new.strip()
    return out


def _text_from_xml(xml_str: str) -> str:
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        warn("core/conversation/quote_text.py:43 ET.ParseError", err=repr(__import__("sys").exc_info()[1]))
        return ""
    parts: list[str] = []
    for tag in ("title", "des"):
        value = str(root.findtext(f".//{tag}", "") or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)


def _appattach_label(xml_str: str, fallback_title: str = "") -> str:
    # <appattach> -> 「[图片:md5]」（图片/动图，md5 由渲染层换成描述）或「[文件: 名字]」。
    block = _APPATTACH_RE.search(xml_str)
    if not block:
        return ""
    inner = block.group(1)
    m_md5 = _AA_MD5_RE.search(inner)
    m_ext = _AA_EXT_RE.search(inner)
    m_title = _AA_TITLE_RE.search(inner)
    md5 = (m_md5.group(1) if m_md5 else "").lower()
    ext = (m_ext.group(1) if m_ext else "").lower()
    title = (m_title.group(1) if m_title else fallback_title or "").strip()
    if ext in _IMAGE_EXTS:
        # 动图/图片：给渲染层留 md5，它才查得到「画面里是什么」
        return "[图片:%s]" % md5 if md5 else "[图片]"
    if title:
        return "[文件: %s]" % title.replace("\n", " ")[:60]
    if ext:
        return "[文件: .%s]" % ext
    return "[文件]"


def clean_quote_text(raw: str) -> str:
    """``refermsg.content`` 原文 -> 可读引用内容（文字优先，媒体才占位）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("<?xml") or text.startswith("<msg>") or text.startswith("<appmsg"):
        if "<appattach" in text:
            label = _appattach_label(text, fallback_title=_text_from_xml(text))
            if label:  # 媒体引用：名字/类型/md5 都比「[文件]」有用
                return label
        inner = _text_from_xml(text)
        if inner:
            stripped = _strip_quote_prefix(inner)
            return stripped or inner
        for marker, label in _MEDIA_MARKERS:
            if marker in text:
                return label
        return "[媒体消息]"
    for marker, label in _MEDIA_MARKERS:
        if marker in text:
            return label
    match = _NAME_PREFIX_RE.match(text)
    if match:
        text = text[match.end():].strip()
    return _strip_quote_prefix(text) or text
