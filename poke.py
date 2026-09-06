"""戳一戳随机表情教学（移植自改版 apps/poke.js）

被戳一戳时按概率（POKE_PROBABILITY，默认 0.7）随机挑一个
「只需头像、无需文字、无必填参数」的模板，用戳一戳发起者的头像
铺满所需图片数量生成表情包，并回复对应的关键词指令。

由 __init__.py 在插件加载末尾导入。与语义表情包插件的戳一戳回复
共存：对方优先级更高（priority 1, block=True），未启用或未命中时
才会轮到这里（priority 2）。
"""

from __future__ import annotations

import random
from typing import List

from nekro_agent.core import logger
from nonebot import on_type
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment, PokeNotifyEvent

from . import _avatar_url, _get_client, config


def _is_poke_target(event: PokeNotifyEvent) -> bool:
    if not config.POKE_ENABLE:
        return False
    return event.is_tome()


poke_matcher = on_type(PokeNotifyEvent, rule=_is_poke_target, priority=2, block=False)


@poke_matcher.handle()
async def handle_bot_poke(bot: Bot, event: PokeNotifyEvent) -> None:
    try:
        if random.random() > config.POKE_PROBABILITY:
            return

        client = _get_client()
        index = await client.get_index()
        if not index:
            return

        poker_id = str(event.user_id)
        try:
            avatar = await client.download_image(_avatar_url(poker_id))
        except Exception as e:
            logger.warning(f"[meme] 戳一戳表情获取头像失败: {e}")
            return

        # 与原版一致：只要头像（min_images >= 1）、不要文字、无自定义参数的模板
        candidates = [
            s
            for s in index.values()
            if s["min_images"] >= 1 and s["max_texts"] == 0 and not s["arg_names"]
        ]
        if not candidates:
            logger.info("[meme] 戳一戳随机表情：没有满足条件的模板")
            return
        random.shuffle(candidates)

        for summary in candidates[:20]:
            try:
                # 与原版一致：同一头像铺满最少所需图片数
                images: List[bytes] = [avatar] * summary["min_images"]
                content = await client.generate(summary["key"], images, [], None)
            except Exception as e:
                logger.debug(f"[meme] 戳一戳表情模板 {summary['key']} 生成失败: {e}")
                continue

            display = summary["keywords"][0] if summary["keywords"] else summary["key"]
            message = Message(
                [
                    MessageSegment.text(f"这是指令：bq{display}\n"),
                    MessageSegment.image(content),
                ]
            )
            try:
                await bot.send(event, message)
                logger.info(f"[meme] 戳一戳随机表情已发送: {summary['key']}")
            except Exception as e:
                logger.warning(f"[meme] 戳一戳表情发送失败: {e}")
            return

        logger.info("[meme] 戳一戳随机表情：所有候选模板均生成失败")
    except Exception as e:
        logger.error(f"[meme] 戳一戳表情处理失败: {e}")
