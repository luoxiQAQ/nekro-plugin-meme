"""Yunzai 风格关键词指令

复刻 Yunzai meme-plugin（forceSharp=true）的聊天触发方式：
  bq摸头 @某人           -> 用关键词对应的模板，取被@用户头像生成
  bq贴 @A @B             -> 多头像模板
  bq举牌 文字1/文字2     -> 带文字模板，文字用 / 分隔
  bq举牌 #text 晚安      -> 附加参数（与原版 #参数名 参数值 语法一致）

素材合成与原版一致：引用回复对象、@用户、消息图片按序取头像/图片，
素材不足时用发送者头像兜底；回复中的图片也可用。

由 __init__.py 在插件加载末尾导入（依赖其中的 client / 配置 / 工具函数）。
"""

from __future__ import annotations

import re
import random
from typing import Any, Dict, List, Optional

from nekro_agent.core import logger
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from . import _avatar_url, _build_args, _get_client, _prepare_texts, config

matcher = on_message(priority=15, block=False)

# 别名索引缓存（关键词/别名 -> 模板摘要），按索引代次失效
_alias_cache: Dict[str, Any] = {}
_alias_cache_index: Dict[str, Any] = {}
_text_arg_re = re.compile(r"#(\S+)\s+([^#]+)")
_text_at_re = re.compile(r"@\s*\d+")
# 「随机表情包」命令（与原版 random.js 的正则一致）
_RANDOM_RE = re.compile(r"^#?(?:(?:清语)?表情|meme(?:-plugin)?)?随机(?:表情|meme)(?:包)?$", re.IGNORECASE)


def _keyword_map() -> Optional[Dict[str, Dict[str, Any]]]:
    """从已构建的模板索引中取别名映射；索引未就绪时返回 None"""
    global _alias_cache_index
    index = _get_client().peek_index()
    if not index:
        return None
    if _alias_cache_index is index:
        return _alias_cache
    kw_map: Dict[str, Dict[str, Any]] = {}
    for summary in index.values():
        for alias in summary["keywords"] + summary["shortcuts"]:
            alias = str(alias).strip().lower()
            if alias:
                kw_map.setdefault(alias, summary)
    _alias_cache.clear()
    _alias_cache.update(kw_map)
    _alias_cache_index = index
    return _alias_cache


def _match_alias(plain: str, kw_map: Dict[str, Dict[str, Any]]) -> Optional[tuple[str, Dict[str, Any]]]:
    """最长别名匹配，且要求关键词后紧跟空白/标点/结尾，避免「一起吃饭」误触发"""
    lower = plain.lower()
    for alias in sorted(kw_map, key=len, reverse=True):
        if not lower.startswith(alias):
            continue
        nxt = plain[len(alias):][:1]
        if nxt and nxt.isalnum():
            continue
        return alias, kw_map[alias]
    return None


@matcher.handle()
async def handle_meme_keyword(bot: Bot, event: MessageEvent) -> None:
    if not config.COMMAND_ENABLE:
        return
    if str(event.user_id) == str(bot.self_id):
        return  # 忽略机器人自己的消息

    plain = event.message.extract_plain_text().strip()
    if not plain:
        return

    # 「随机表情包」命令：原版 random.js 无视 forceSharp，直接裸词即可触发
    if _RANDOM_RE.match(plain) or (plain.lower().startswith("bq") and _RANDOM_RE.match(plain[2:].strip())):
        await _handle_random_meme(bot, event)
        return

    prefix = config.COMMAND_FORCE_PREFIX.strip().lower()
    if prefix:
        # 对应 Yunzai forceSharp=true：必须带前缀
        if not plain.lower().startswith(prefix):
            return
        plain = plain[len(prefix):].strip()
        if not plain:
            return

    try:
        kw_map = _keyword_map()
        if kw_map is None:
            # 索引尚未构建完成（如刚重启），等待构建后重试
            await _get_client().get_index()
            kw_map = _keyword_map()
    except Exception as e:
        logger.warning(f"[meme] 关键词索引获取失败: {e}")
        return
    if not kw_map:
        return

    matched = _match_alias(plain, kw_map)
    if matched is None:
        return
    alias, summary = matched
    key = summary["key"]
    rest = plain[len(alias):].strip()

    # 触发表情回应（移植自改版提交「简化QQ平台表情回应逻辑」，需要 napcat 支持）
    if config.REACTION_ENABLE:
        try:
            await bot.call_api("set_msg_emoji_like", message_id=event.message_id, emoji_id="66")
        except Exception:
            pass

    # 解析 #参数名 参数值（与原版 args 语法一致），并剔除文本中的 @QQ号
    args_parsed: Dict[str, Any] = {}
    for m in _text_arg_re.finditer(rest):
        args_parsed[m.group(1)] = m.group(2).strip()
    rest = _text_arg_re.sub("", rest)
    rest = _text_at_re.sub("", rest).strip()
    texts = [t.strip() for t in rest.split("/") if t.strip()]

    # 收集素材：引用回复对象 > @用户 > 消息图片；不足时用发送者头像兜底
    # 注意：OneBot 适配器会把「@机器人」的 at 段从 event.message 中移除（转成 to_me 标记），
    # 因此必须从 original_message（原始消息）提取，否则 @机器人 当素材会拿不到
    raw_message = getattr(event, "original_message", None) or event.message
    reply = getattr(event, "reply", None)
    quoted_id = str(reply.sender.user_id) if reply is not None and reply.sender else None
    at_ids = [
        str(seg.data.get("qq"))
        for seg in raw_message
        if seg.type == "at" and str(seg.data.get("qq")) not in ("all", "0")
    ]
    all_users = ([quoted_id] if quoted_id else []) + [u for u in at_ids if u != quoted_id]
    image_urls = [
        str(seg.data.get("url"))
        for seg in raw_message
        if seg.type == "image" and seg.data.get("url")
    ]

    client = _get_client()
    images: List[bytes] = []
    for uid in all_users:
        try:
            images.append(await client.download_image(_avatar_url(uid)))
        except Exception as e:
            logger.warning(f"[meme] 获取用户 {uid} 头像失败: {e}")
    if summary["min_images"] > len(images) and len(image_urls) > 0:
        for url in image_urls:
            try:
                images.append(await client.download_image(url))
            except Exception as e:
                logger.warning(f"[meme] 下载消息图片失败: {e}")

    # 素材不足时，先把文字里的纯数字 QQ 号当作头像来源（如 bq贴贴 123456）
    if summary["min_images"] > len(images):
        for uid in [t for t in texts if t.isdigit() and 5 <= len(t) <= 12]:
            if summary["min_images"] <= len(images):
                break
            try:
                images.append(await client.download_image(_avatar_url(uid)))
                texts.remove(uid)
            except Exception as e:
                logger.warning(f"[meme] 获取 QQ {uid} 头像失败: {e}")

    # 仍不足：用发送者头像兜底（与原版行为一致）
    if summary["min_images"] > len(images):
        try:
            images.insert(0, await client.download_image(_avatar_url(str(event.user_id))))
        except Exception as e:
            logger.warning(f"[meme] 获取发送者头像失败: {e}")

    if summary["max_images"] > 0 and len(images) < summary["min_images"]:
        need = (
            f"{summary['min_images']} 张图片"
            if summary["min_images"] == summary["max_images"]
            else f"{summary['min_images']}~{summary['max_images']} 张图片"
        )
        await _reply(bot, event, f"「{alias}」需要{need}：@要拍的人或发一张图片再试试（你自己会自动算一张）")
        logger.info(f"[meme] 关键词指令素材不足: {alias} 需要 {need}，实际 {len(images)} 张")
        return
    if summary["max_images"] > 0 and len(images) > summary["max_images"]:
        images = images[: summary["max_images"]]

    final_texts = _prepare_texts(summary, texts)
    if summary["max_texts"] > 0 and not summary["min_texts"] <= len(final_texts) <= summary["max_texts"]:
        await _reply(
            bot,
            event,
            f"「{alias}」需要 {summary['min_texts']}~{summary['max_texts']} 条文字，用 / 分隔，例如 bq{alias} 文字1/文字2",
        )
        return

    # user_infos 昵称：仅模板带参数时才取
    user_name = ""
    if summary["has_args"]:
        try:
            info = await bot.get_stranger_info(user_id=int(all_users[0] if all_users else event.user_id))
            user_name = str(info.get("nick") or "")
        except Exception:
            user_name = ""
    args_json, _notes = _build_args(summary, args_parsed, user_name, "unknown")

    try:
        content = await client.generate(key, images, final_texts, args_json)
    except Exception as e:
        logger.warning(f"[meme] 关键词指令生成失败 ({key}): {e}")
        await _reply(bot, event, f"表情包生成失败：{str(e)[:120]}")
        return

    try:
        await bot.send(event, MessageSegment.image(content))
        logger.info(f"[meme] 关键词指令生成成功: {alias} -> {key}")
    except Exception as e:
        logger.error(f"[meme] 表情包发送失败 ({key}): {e}")


async def _reply(bot: Bot, event: MessageEvent, text: str) -> None:
    try:
        await bot.send(event, text)
    except Exception as e:
        logger.warning(f"[meme] 提示消息发送失败: {e}")


async def _handle_random_meme(bot: Bot, event: MessageEvent) -> None:
    """「随机表情包」命令（移植自改版 apps/random.js）

    随机挑选一个「只需 1 条文字」或「只需 1 张图片」的模板，用发送者头像 /
    模板默认文字生成，并展示该模板的关键词指令。
    """
    client = _get_client()
    try:
        index = await client.get_index()
    except Exception as e:
        await _reply(bot, event, f"生成随机表情失败：{e}")
        return

    candidates = [
        s
        for s in index.values()
        if (s["min_texts"] == 1 and s["max_texts"] == 1)
        or (s["min_images"] == 1 and s["max_images"] == 1)
    ]
    if not candidates:
        await _reply(bot, event, "未找到可用的表情包模板")
        return

    random.shuffle(candidates)
    avatar: Optional[bytes] = None
    try:
        avatar = await client.download_image(_avatar_url(str(event.user_id)))
    except Exception as e:
        logger.warning(f"[meme] 随机表情获取头像失败: {e}")

    for summary in candidates[:30]:
        try:
            images: List[bytes] = [avatar] if summary["min_images"] == 1 and avatar else []
            if summary["max_images"] > 0 and not summary["min_images"] <= len(images) <= summary["max_images"]:
                continue
            final_texts = _prepare_texts(summary, [])
            if summary["max_texts"] > 0 and not summary["min_texts"] <= len(final_texts) <= summary["max_texts"]:
                continue
            args_json, _ = _build_args(summary, None, "", "unknown")
            content = await client.generate(summary["key"], images, final_texts, args_json)
        except Exception as e:
            logger.debug(f"[meme] 随机表情模板 {summary['key']} 生成失败: {e}")
            continue

        aliases = " ".join(f"[{k}]" for k in summary["keywords"][:8]) or "[无]"
        message = Message(
            [
                MessageSegment.text(
                    f"本次随机表情信息如下:\n表情的名称: {summary['key']}\n表情的别名: {aliases}\n"
                ),
                MessageSegment.image(content),
            ]
        )
        try:
            await bot.send(event, message)
            logger.info(f"[meme] 随机表情已发送: {summary['key']}")
        except Exception as e:
            logger.warning(f"[meme] 随机表情发送失败: {e}")
        return

    await _reply(bot, event, "生成随机表情失败：未找到可用的表情包")
