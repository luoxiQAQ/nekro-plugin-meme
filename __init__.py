"""meme 表情包插件（移植自 Yunzai meme-plugin）

将 MemeCrafters/meme-generator 表情包生成服务接入 Nekro：
- Agent 可搜索/查看表情包模板，并按模板要求组合 QQ 头像、聊天图片与文字生成表情包
- 生成的图片直接回传沙盒并发送到当前聊天
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger

from .api_client import MemeAPIError, MemeClient

plugin = NekroPlugin(
    name="meme 表情包工作坊",
    module_name="nekro_plugin_meme",
    description="接入 meme-generator 表情包服务（移植自 Yunzai meme-plugin）：可搜索 800+ 表情包模板，为指定 QQ 用户头像、聊天图片与文字生成表情包（如 petpet、摸头、举起等），并直接发送到聊天。",
    version="1.0.0",
    author="luoxi",
    url="https://github.com/luoxiQAQ/nekro-plugin-meme",
    support_adapter=["onebot_v11"],
)


@plugin.mount_config()
class MemeConfig(ConfigBase):
    MEME_API_BASE: str = Field(
        default="http://172.17.0.1:2233",
        title="meme-generator API 地址",
        description="MemeCrafters meme-generator API 服务地址（需 nekro_agent 容器可达）。",
    )
    REQUEST_TIMEOUT: int = Field(
        default=90,
        title="请求超时（秒）",
        description="调用 meme-generator API 与下载图片的超时时间。",
    )
    AVATAR_SIZE: int = Field(
        default=640,
        title="QQ 头像尺寸",
        description="获取 QQ 头像使用的尺寸参数，可选 40 / 100 / 140 / 640。",
    )
    DEFAULT_USER_NAME: str = Field(
        default="用户",
        title="默认用户名",
        description="模板需要 user_infos 参数且未提供用户名时使用的默认名称。",
    )
    COMMAND_ENABLE: bool = Field(
        default=True,
        title="关键词指令开关",
        description="开启后可在聊天中直接发送「bq关键词」（如 bq摸头 @某人）触发表情包生成，与 Yunzai meme-plugin 用法一致。",
    )
    COMMAND_FORCE_PREFIX: str = Field(
        default="bq",
        title="关键词指令前缀",
        description="关键词触发的前缀（对应 Yunzai forceSharp）。留空则直接发送关键词即可触发（容易误触发），默认 bq。",
    )
    REACTION_ENABLE: bool = Field(
        default=False,
        title="关键词触发表情回应",
        description="关键词指令触发时给消息添加表情回应（QQ 表情 66，需要 napcat 支持 set_msg_emoji_like）。",
    )
    POKE_ENABLE: bool = Field(
        default=True,
        title="戳一戳随机表情",
        description="被戳一戳时按概率随机生成一张只需头像的表情包，并附上对应关键词指令（移植自改版 apps/poke.js）。",
    )
    POKE_PROBABILITY: float = Field(
        default=0.7,
        title="戳一戳触发概率",
        description="被戳一戳后触发随机表情的概率，0~1（对应改版新增的 pokeProbability 配置）。",
        ge=0.0,
        le=1.0,
    )
    PROMPT_GUIDE_ENABLE: bool = Field(
        default=True,
        title="AI 主动表情包引导",
        description="在对话提示中注入表情包使用引导，让 Agent 在聊天中想表达抱抱、贴贴等互动时主动生成并发表情包。",
    )


config: MemeConfig = plugin.get_config(MemeConfig)

_client: Optional[MemeClient] = None
_warm_task: Optional["asyncio.Task"] = None


def _get_client() -> MemeClient:
    global _client
    if _client is None:
        _client = MemeClient(
            base_url=config.MEME_API_BASE,
            timeout=float(config.REQUEST_TIMEOUT),
            cache_dir=Path(plugin.get_plugin_data_dir()),
        )
    return _client


def _avatar_url(qq: str) -> str:
    return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s={config.AVATAR_SIZE}"


async def _self_qq() -> Optional[str]:
    """获取机器人自身的 QQ 号（nonebot 运行时可用）"""
    try:
        from nonebot import get_bot

        return str(get_bot().self_id)
    except Exception as e:
        logger.warning(f"[meme] 获取机器人自身 QQ 号失败: {e}")
        return None


async def _resolve_images(_ctx: AgentCtx, user_ids: List[str], image_paths: List[str]) -> List[bytes]:
    """按 Yunzai meme-plugin 规则合成图片列表：@用户的头像在前，消息图片在后

    user_ids 支持哨兵值 "me" / "bot" / "我" / "自己"，代表机器人自身头像。
    """
    client = _get_client()
    images: List[bytes] = []

    for user_id in user_ids:
        qq = str(user_id).strip()
        if not qq:
            continue
        if qq.lower() in ("me", "bot", "我", "自己"):
            self_qq = await _self_qq()
            if not self_qq:
                continue
            qq = self_qq
        try:
            images.append(await client.download_image(_avatar_url(qq)))
        except Exception as e:
            logger.warning(f"[meme] 获取 QQ {qq} 头像失败: {e}")

    for path in image_paths:
        item = str(path).strip()
        if not item:
            continue
        try:
            if item.isdigit() and 5 <= len(item) <= 12:
                images.append(await client.download_image(_avatar_url(item)))
            elif item.startswith(("http://", "https://")):
                images.append(await client.download_image(item))
            else:
                host_path = Path(_ctx.fs.get_file(item))
                images.append(host_path.read_bytes())
        except MemeAPIError:
            raise
        except Exception as e:
            logger.warning(f"[meme] 读取图片 {item} 失败: {e}")

    return images


def _format_summary(summary: Dict[str, Any]) -> str:
    name = summary["keywords"][0] if summary["keywords"] else summary["key"]
    req = f"图片{summary['min_images']}~{summary['max_images']}张, 文字{summary['min_texts']}~{summary['max_texts']}条"
    aliases = "/".join(summary["keywords"][:5])
    return f"[{summary['key']}] {name}（{req}）关键词: {aliases}"


def _prepare_texts(summary: Dict[str, Any], texts: List[str]) -> List[str]:
    """补齐文字：不足时用模板默认文字随机填充（与 Yunzai meme-plugin 行为一致）"""
    final_texts = [str(t).strip() for t in texts if str(t).strip()][: summary["max_texts"]]
    defaults = summary["default_texts"]
    if len(final_texts) < summary["min_texts"] and defaults:
        import random

        while len(final_texts) < summary["min_texts"]:
            final_texts.append(random.choice(defaults))
    return final_texts


def _check_counts(summary: Dict[str, Any], n_images: int, n_texts: int) -> None:
    key = summary["key"]
    if summary["max_images"] > 0 and not summary["min_images"] <= n_images <= summary["max_images"]:
        raise RuntimeError(
            f"模板 '{key}' 需要 {summary['min_images']}~{summary['max_images']} 张图片，当前提供了 {n_images} 张。"
            "请通过 user_ids（QQ号头像）或 image_paths（图片路径/URL）补充素材。可调用 get_meme_info 查看模板要求。"
        )
    if summary["max_texts"] > 0 and not summary["min_texts"] <= n_texts <= summary["max_texts"]:
        raise RuntimeError(
            f"模板 '{key}' 需要 {summary['min_texts']}~{summary['max_texts']} 条文字，当前提供了 {n_texts} 条。"
            "请通过 texts 参数补充文字（多条文字直接用列表传入）。可调用 get_meme_info 查看模板要求。"
        )


def _build_args(
    summary: Dict[str, Any],
    args: Optional[Dict[str, Any]],
    user_name: str,
    user_gender: str,
) -> tuple[Optional[str], str]:
    """构造 args JSON（与 Yunzai meme-plugin 一致：user_infos + 模板自定义参数）

    meme-generator 的 args 基础模型始终包含 user_infos 字段，因此只要有 args_type
    就可以安全携带 user_infos；自定义参数名则必须与模板声明完全一致。
    """
    if not summary["has_args"]:
        return None, ""

    provided = dict(args or {})
    notes = ""

    unknown = [k for k in provided if k not in summary["arg_names"] and k != "user_infos"]
    if unknown:
        notes = f"已忽略不支持参数: {', '.join(unknown)}（该模板支持: {', '.join(summary['arg_names']) or '仅 user_infos'}）"
        for k in unknown:
            provided.pop(k, None)

    if "user_infos" not in provided:
        provided["user_infos"] = [
            {"name": user_name or config.DEFAULT_USER_NAME, "gender": user_gender or "unknown"}
        ]

    return json.dumps(provided, ensure_ascii=False), notes


@plugin.mount_prompt_inject_method(
    name="meme_表情包主动使用引导",
    description="告诉 Agent 拥有表情包生成能力，并引导其在合适的情感节点主动使用",
)
async def _prompt_inject_meme_guide(_ctx: AgentCtx) -> str:
    if not config.PROMPT_GUIDE_ENABLE:
        return ""
    return (
        "【表情包能力】你可以生成并发送表情包来表达肢体互动和情绪（抱抱、贴贴、亲亲、拍头、举牌等）。"
        "在情感合适的时候（安慰、撒娇、得意、调侃、表示喜爱等）主动发一张，聊天会更生动：\n"
        '- 想抱住对方: await generate_meme("hug", user_ids=["me", 对方的QQ号])  # hug 是你抱对方，第一张图是你自己\n'
        '- 想贴贴: await generate_meme("rub", user_ids=["me", 对方的QQ号])\n'
        '- 想亲亲: await generate_meme("kiss", user_ids=["me", 对方的QQ号])\n'
        '- 只需对方头像的模板: await generate_meme("hold_tight", user_ids=[对方的QQ号])  # 抱紧；类似还有 hug_leg 抱大腿、mengqin 猛亲、petpet 拍头\n'
        '- 举牌写字: await generate_meme("raise_sign", user_ids=[对方的QQ号], texts=["想写的话"])\n'
        '- 拿不准有哪些模板: await search_meme("抱") 按关键词搜索，或 await random_meme([对方的QQ号]) 随机来一张\n'
        "使用要点：\n"
        "1. 对方的QQ号可从聊天上下文中消息旁边的用户ID获取。\n"
        '2. user_ids 中的 "me" 代表你自己（机器人）的头像；两个图的模板第一张是动作发出方。\n'
        "3. generate_meme 会自动把表情包发到当前聊天，不需要再调用 send_image。\n"
        "4. 不要在回复文本里写 bq 之类的指令词，直接调用方法发图即可；也不要每条消息都用，避免刷屏。\n"
        "5. 生成失败时按报错提示调整图片/文字数量，或先 search_meme 换个模板。"
    )


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="搜索表情包模板",
    description="按关键词搜索可用的表情包模板，返回模板 key、名称、所需图片/文字数量。生成表情包前应先调用本方法找到合适的模板 key。",
)
async def search_meme(
    keyword: str = "",
    limit: int = 10,
) -> str:
    """搜索表情包模板

    按关键词（支持中文别名，如 "拍"、"摸头"、"举"、"贴"、"亲"）搜索表情包模板。
    keyword 留空时随机返回一批可用模板。

    Args:
        keyword: 搜索关键词，支持模板 key、中文名称、别名的模糊匹配；留空则随机推荐
        limit: 最多返回的模板数量（1~30）

    Returns:
        str: 模板列表文本，每行格式为 "[key] 名称（图片x~y张, 文字x~y条）关键词: ..."

    Example:
        summaries = await search_meme("摸头")
    """
    limit = max(1, min(int(limit), 30))
    index = await _get_client().get_index()
    results = _get_client().search(keyword, limit)
    if not results:
        return f"没有找到与 '{keyword}' 相关的表情包模板，可尝试其他关键词（共 {len(index)} 个模板可用）。"
    lines = [_format_summary(summary) for summary in results]
    hint = "\n提示: 使用 generate_meme(key=..., ...) 生成，用 get_meme_info(key) 查看模板详细要求。"
    return f"共找到 {len(results)} 个模板（索引共 {len(index)} 个）:\n" + "\n".join(lines) + hint


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="查看表情包模板详情",
    description="查看某个表情包模板的详细参数要求：需要几张图片几张文字、默认文字、附加参数（args）的名称与说明。",
)
async def get_meme_info(key: str) -> str:
    """查看表情包模板详情

    Args:
        key: 模板 key（由 search_meme 返回）

    Returns:
        str: 模板参数要求说明

    Example:
        info = await get_meme_info("petpet")
    """
    index = await _get_client().get_index()
    summary = index.get(key)
    if summary is None:
        raise RuntimeError(f"模板 '{key}' 不存在，请先调用 search_meme 搜索可用模板。")

    lines = [
        f"模板 key: {summary['key']}",
        f"关键词: {'/'.join(summary['keywords']) or '（无）'}",
        f"别名: {'/'.join(summary['shortcuts']) or '（无）'}",
        f"图片需求: {summary['min_images']}~{summary['max_images']} 张",
        f"文字需求: {summary['min_texts']}~{summary['max_texts']} 条",
    ]
    if summary["default_texts"]:
        lines.append(f"默认文字: {' | '.join(summary['default_texts'])}")
    if summary["arg_names"]:
        lines.append("附加参数 (args):")
        for name in summary["arg_names"]:
            desc = summary["arg_desc"].get(name)
            lines.append(f"  - {name}: {desc or '（无说明）'}")
        if summary["arg_examples"]:
            lines.append(f"  参数示例: {json.dumps(summary['arg_examples'][0], ensure_ascii=False)}")
    else:
        lines.append("附加参数: 无")
    return "\n".join(lines)


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="生成表情包",
    description="用表情包模板生成图片并可直接发送到聊天。图片素材来自 QQ 用户头像（user_ids）或聊天中的图片（image_paths），部分模板还需要文字（texts）。素材不足时会报错并说明模板要求。",
)
async def generate_meme(
    _ctx: AgentCtx,
    key: str,
    user_ids: Optional[List[str]] = None,
    image_paths: Optional[List[str]] = None,
    texts: Optional[List[str]] = None,
    user_name: str = "",
    user_gender: Literal["male", "female", "unknown"] = "unknown",
    args: Optional[Dict[str, Any]] = None,
    send_to_chat: bool = True,
) -> str:
    """生成表情包

    使用模板生成表情包。素材合成规则与 Yunzai meme-plugin 一致：先取 user_ids 对应的 QQ 头像，
    再取 image_paths 中的图片（沙盒路径 / http(s) URL / 纯数字会当作 QQ 号取头像）。
    当未提供任何图片时自动使用触发者（当前用户）头像兜底。

    Args:
        key: 模板 key，由 search_meme 获得
        user_ids: QQ 号列表，按顺序取其头像作为图片素材（如 [被拍的人, 拍人的人]）
        image_paths: 图片路径或 URL 列表；纯数字字符串会被当作 QQ 号取头像
        texts: 文字列表，多段文字用列表按顺序传入（仅部分模板需要）
        user_name: 模板渲染 user_infos 时使用的名字（如当前说话用户的昵称）
        user_gender: 模板渲染 user_infos 时使用的性别
        args: 模板附加参数字典（用 get_meme_info 查看可用参数）
        send_to_chat: 是否把生成的表情包直接发送到当前聊天

    Returns:
        str: 生成的表情包沙盒路径（已发送时附说明）

    Example:
        # 拍一拍指定用户
        path = await generate_meme("petpet", user_ids=["123456"])
        # 用聊天里的两张图生成
        path = await generate_meme("kiss", image_paths=["/path/img1.jpg", "/path/img2.jpg"])
        # 需要文字的模板
        path = await generate_meme("load", texts=["好好学习"], user_ids=["123456"])
    """
    client = _get_client()
    index = await client.get_index()
    summary = index.get(key)
    if summary is None:
        raise RuntimeError(f"模板 '{key}' 不存在，请先调用 search_meme 搜索可用模板。")

    user_ids = [str(u) for u in (user_ids or []) if str(u).strip()]
    image_paths = [str(p) for p in (image_paths or []) if str(p).strip()]

    images = await _resolve_images(_ctx, user_ids, image_paths)

    # 未提供任何图片且模板需要图片时，用触发者头像兜底（放最前，与 Yunzai 顺序一致）
    if not images and summary["min_images"] > 0 and _ctx.from_platform_userid:
        try:
            images.insert(0, await client.download_image(_avatar_url(str(_ctx.from_platform_userid))))
        except Exception as e:
            logger.warning(f"[meme] 获取触发者头像失败: {e}")

    # 图片超出上限时截断（头像优先，与 Yunzai 一致）
    if summary["max_images"] > 0 and len(images) > summary["max_images"]:
        images = images[: summary["max_images"]]

    final_texts = _prepare_texts(summary, [str(t) for t in (texts or [])])
    _check_counts(summary, len(images), len(final_texts))

    args_json, notes = _build_args(summary, args, user_name, user_gender)

    try:
        content = await client.generate(key, images, final_texts, args_json)
    except MemeAPIError as e:
        raise RuntimeError(
            f"{e}。可调用 get_meme_info('{key}') 核对素材数量与参数要求。"
        ) from e

    sandbox_path = await _ctx.fs.mixed_forward_file(
        content, file_name=f"meme_{key}_{int(time.time())}.png"
    )

    sent_note = ""
    if send_to_chat:
        try:
            await _ctx.send_image(sandbox_path)
            sent_note = "，已发送到当前聊天"
        except Exception as e:
            logger.warning(f"[meme] 发送表情包失败: {e}")
            sent_note = f"，发送到聊天失败（{e}），请用 send_image 自行发送"

    logger.info(f"[meme] 生成表情包成功: {key} -> {sandbox_path}")
    result = f"表情包生成成功{sent_note}: {sandbox_path}"
    if notes:
        result += f"\n注意: {notes}"
    return result


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="随机生成表情包",
    description="随机挑选一个适合头像的模板，为指定 QQ 用户（默认当前用户）生成一张表情包，制造趣味互动效果。",
)
async def random_meme(
    _ctx: AgentCtx,
    user_ids: Optional[List[str]] = None,
    send_to_chat: bool = True,
) -> str:
    """随机生成表情包

    随机选择一个只需要头像、不需要文字的模板生成表情包。
    user_ids 为空时使用当前触发者的头像。

    Args:
        user_ids: QQ 号列表（可选），为空则使用当前用户头像
        send_to_chat: 是否直接发送到当前聊天

    Returns:
        str: 生成的表情包沙盒路径

    Example:
        path = await random_meme(["123456"])
    """
    import random

    user_ids = [str(u) for u in (user_ids or []) if str(u).strip()]
    if not user_ids and _ctx.from_platform_userid:
        user_ids = [str(_ctx.from_platform_userid)]

    images = await _resolve_images(_ctx, user_ids, [])
    if not images:
        raise RuntimeError("无法获取用户头像，请提供 user_ids 或图片路径。")

    index = await _get_client().get_index()
    candidates = [
        summary
        for summary in index.values()
        if summary["min_texts"] == 0
        and summary["min_images"] <= len(images) <= summary["max_images"]
    ]
    if not candidates:
        raise RuntimeError("当前没有与素材数量匹配的头像类模板。")
    summary = random.choice(candidates)

    if len(images) > summary["max_images"]:
        images = images[: summary["max_images"]]

    try:
        content = await _get_client().generate(summary["key"], images, [])
    except MemeAPIError as e:
        raise RuntimeError(f"{e}") from e

    sandbox_path = await _ctx.fs.mixed_forward_file(
        content, file_name=f"meme_{summary['key']}_{int(time.time())}.png"
    )

    sent_note = ""
    if send_to_chat:
        try:
            await _ctx.send_image(sandbox_path)
            sent_note = "，已发送到当前聊天"
        except Exception as e:
            logger.warning(f"[meme] 发送表情包失败: {e}")
            sent_note = f"，发送到聊天失败（{e}），请用 send_image 自行发送"

    logger.info(f"[meme] 随机表情包 {summary['key']} -> {sandbox_path}")
    return f"随机模板 [{summary['key']}] 表情包生成成功{sent_note}: {sandbox_path}"


@plugin.mount_init_method()
async def _warm_index() -> None:
    """后台预热模板索引缓存，不阻塞插件加载"""
    global _warm_task
    _warm_task = asyncio.create_task(_warm_index_job())


async def _warm_index_job() -> None:
    try:
        index = await _get_client().get_index()
        logger.info(f"[meme] 模板索引预热完成，共 {len(index)} 个模板")
    except Exception as e:
        logger.warning(f"[meme] 模板索引预热失败（首次使用时会重试）: {e}")


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    """清理插件资源"""
    global _client, _warm_task
    if _warm_task is not None and not _warm_task.done():
        _warm_task.cancel()
    _warm_task = None
    _client = None
    logger.info("meme Plugin Resources Cleaned Up")


# 加载聊天触发模块（必须放在最后：依赖上面的插件实例、配置与工具函数）
try:
    from . import commands  # noqa: F401

    logger.info("[meme] 关键词指令模块已加载（触发方式见插件配置）")
except Exception:
    logger.exception("[meme] 关键词指令模块加载失败，聊天关键词触发不可用")

try:
    from . import poke  # noqa: F401

    logger.info("[meme] 戳一戳随机表情模块已加载")
except Exception:
    logger.exception("[meme] 戳一戳随机表情模块加载失败")
