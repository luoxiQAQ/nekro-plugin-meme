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
    # Agent 工具适用于 Web、OneBot 等所有对话适配器；发送由当前上下文处理。
    support_adapter=[],
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


async def _download_avatar_with_retry(client: MemeClient, qq: str, max_retries: int = 3) -> Optional[bytes]:
    """下载 QQ 头像，带重试和备用 URL"""
    import httpx as _httpx

    urls = [
        _avatar_url(qq),
        f"https://q2.qlogo.cn/g?b=qq&nk={qq}&s=640",
        f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640",
    ]
    last_err = None
    for attempt in range(max_retries):
        url = urls[attempt % len(urls)]
        try:
            return await client.download_image(url)
        except Exception as e:
            last_err = e
            logger.warning(f"[meme] 头像下载第 {attempt + 1} 次失败 (QQ {qq}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
    # 最后尝试用独立的 httpx 客户端直接下载
    try:
        async with _httpx.AsyncClient(timeout=15, follow_redirects=True) as hc:
            resp = await hc.get(_avatar_url(qq))
            if resp.status_code < 400 and resp.content:
                return resp.content
    except Exception as e:
        logger.warning(f"[meme] 独立客户端下载头像也失败 (QQ {qq}): {e}")
    logger.warning(f"[meme] 获取 QQ {qq} 头像最终失败: {last_err}")
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
        avatar = await _download_avatar_with_retry(client, qq)
        if avatar:
            images.append(avatar)

    for path in image_paths:
        item = str(path).strip()
        if not item:
            continue
        try:
            if item.isdigit() and 5 <= len(item) <= 12:
                avatar = await _download_avatar_with_retry(client, item)
                if avatar:
                    images.append(avatar)
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
    prefix = config.COMMAND_FORCE_PREFIX.strip() or "bq"

    # 动态从索引获取常用关键词摘要
    keyword_summary = ""
    try:
        client = _get_client()
        index = client.peek_index()
        if index:
            total = len(index)
            # 按类别整理常用模板
            categories = {
                "互动动作": [],
                "头像特效": [],
                "趣味恶搞": [],
                "文字模板": [],
            }
            # 互动类关键词（需要2张图，适合 @对方）
            interaction_keys = [
                "hug", "rub", "kiss", "hold_tight", "mengqin", "fencing",
                "together", "call_110", "daynight", "play_together",
                "captain", "pepe_raise", "whip", "motivate",
            ]
            # 单图特效类
            effect_keys = [
                "petpet", "roll", "turn", "bite", "knock", "pound",
                "throw", "eat", "suck", "pinch", "thump", "smash",
                "worship", "shock", "garbage", "support", "need",
                "little_angel", "confuse", "clown", "prpr",
                "beat_head", "scratch_head", "flash_blind",
                "trance", "addiction", "bubble_tea",
            ]
            # 文字模板类
            text_keys = [
                "bronya_holdsign", "ayachi_holdsign", "ba_say",
                "high_EQ", "luoyonghao_say", "pornhub",
                "note_for_leave", "fanatic",
            ]
            for k in interaction_keys:
                if k in index:
                    kws = index[k].get("keywords", [])
                    if kws:
                        categories["互动动作"].append(kws[0])
            for k in effect_keys:
                if k in index:
                    kws = index[k].get("keywords", [])
                    if kws:
                        categories["头像特效"].append(kws[0])
            for k in text_keys:
                if k in index:
                    kws = index[k].get("keywords", [])
                    if kws:
                        categories["文字模板"].append(kws[0])

            lines = [f"后端共有 {total} 个表情包模板，常用的有："]
            for cat, kws in categories.items():
                if kws:
                    lines.append(f"  {cat}: {'、'.join(kws)}")
            lines.append("  以上仅是冰山一角，用 search_meme(关键词) 可发现更多模板。")
            keyword_summary = "\n".join(lines)
    except Exception:
        keyword_summary = "后端有 800+ 表情包模板，用 search_meme(关键词) 可搜索发现。"

    if not keyword_summary:
        keyword_summary = "后端有 800+ 表情包模板，用 search_meme(关键词) 可搜索发现。"

    return (
        "【表情包能力】你可以生成并发送表情包来表达肢体互动和情绪。\n"
        "在情感合适的时候（安慰、撒娇、得意、调侃、表示喜爱等）主动发一张，聊天会更生动。\n"
        "\n"
        f"{keyword_summary}\n"
        "\n"
        "【使用流程】\n"
        "1. 想用表情包时，先确认关键词是否存在：await search_meme(\"你想用的关键词\")\n"
        "2. 确认模板存在后，调用 send_meme_command 发送：\n"
        f'   - 互动类（抱/贴/亲/拍等）: await send_meme_command("关键词", 对方QQ号)\n'
        f'   - 需要文字的模板: await send_meme_command("关键词", 对方QQ号, text="文字内容")\n'
        f'   - 不需要@人的: await send_meme_command("关键词")\n'
        "\n"
        "【常用示例】\n"
        '- 抱住对方: await send_meme_command("抱", 对方QQ号)\n'
        '- 贴贴: await send_meme_command("贴", 对方QQ号)\n'
        '- 亲亲: await send_meme_command("亲", 对方QQ号)\n'
        '- 摸头: await send_meme_command("摸", 对方QQ号)\n'
        '- 拍头: await send_meme_command("拍", 对方QQ号)\n'
        '- 捏: await send_meme_command("捏", 对方QQ号)\n'
        '- 吃: await send_meme_command("吃", 对方QQ号)\n'
        '- 舔: await send_meme_command("舔", 对方QQ号)\n'
        '- 锤: await send_meme_command("锤", 对方QQ号)\n'
        '- 丢: await send_meme_command("丢", 对方QQ号)\n'
        '- 精神支柱: await send_meme_command("精神支柱", 对方QQ号)\n'
        '- 小天使: await send_meme_command("小天使", 对方QQ号)\n'
        '- 举牌写字: await send_meme_command("举牌", text="想写的话")\n'
        '- 不确定关键词: 先 await search_meme("想搜的词") 再决定\n'
        "\n"
        "【重要规则】\n"
        "1. 当用户明确要求生成、发送或使用表情包时，必须调用本插件工具，不要自己编写 Python/PIL 代码替代，也不要声称插件未挂载。\n"
        "2. 对方的QQ号从聊天上下文中消息旁的用户ID获取。\n"
        "3. send_meme_command 会先发 bq指令文本再发图片，全自动处理。\n"
        "4. 一条消息只调用一次 send_meme_command，不要重复调用！\n"
        "5. 不要在你的回复文本里写 bq 指令词，交给 send_meme_command。\n"
        "6. 别每条消息都发表情包，适度使用。\n"
        '7. 如果用户要求"生成一个表情包"但没指定类型，先用 search_meme 搜索或用 random_meme 随机生成。\n'
        "8. text 参数只用于模板需要的文字内容（如举牌上要写的话），不要把你的聊天回复放进 text。"
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




# 防重复调用记录: chat_key -> (keyword, timestamp)
_meme_dedup: Dict[str, tuple] = {}
_MEME_DEDUP_WINDOW = 15  # 秒，同一聊天同一关键词在此时间窗口内不重复发送


async def _send_msg_direct(chat_key: str, message) -> None:
    """通过 nonebot bot API 直接发送消息（文本或 Message 对象），绕过沙箱消息合并管线"""
    import re as _re
    from nonebot import get_bot

    bot = get_bot()
    m = _re.match(r"onebot_v11-group_(\d+)", chat_key)
    if m:
        group_id = int(m.group(1))
        await bot.call_api("send_group_msg", group_id=group_id, message=message)
    else:
        m = _re.match(r"onebot_v11-private_(\d+)", chat_key)
        if m:
            user_id = int(m.group(1))
            await bot.call_api("send_private_msg", user_id=user_id, message=message)
        else:
            raise RuntimeError(f"不支持的 chat_key 格式: {chat_key}")


async def _send_image_direct(chat_key: str, image_content: bytes) -> None:
    """通过 nonebot bot API 直接发送图片，绕过沙盒消息合并管线"""
    import re as _re
    from nonebot import get_bot
    from nonebot.adapters.onebot.v11 import MessageSegment

    bot = get_bot()
    m = _re.match(r"onebot_v11-group_(\d+)", chat_key)
    if m:
        group_id = int(m.group(1))
        await bot.call_api("send_group_msg", group_id=group_id, message=MessageSegment.image(image_content))
    else:
        m = _re.match(r"onebot_v11-private_(\d+)", chat_key)
        if m:
            user_id = int(m.group(1))
            await bot.call_api("send_private_msg", user_id=user_id, message=MessageSegment.image(image_content))
        else:
            raise RuntimeError(f"不支持的 chat_key 格式: {chat_key}")


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="发送表情包指令",
    description="用 Yunzai 风格发送表情包：先发一条 bq 指令文本，再自动生成并发送对应的表情包图片。推荐 AI 主动发表情包时使用此方法。",
)
async def send_meme_command(
    _ctx: AgentCtx,
    keyword: str = "抱",
    target_qq: str = "",
    text: str = "",
) -> str:
    """发送表情包指令（Yunzai 风格）

    先在聊天中发一条"bq关键词 @QQ号"的文本指令，然后自动查找对应的模板、
    生成表情包图片并直接发送到聊天。效果与 Yunzai meme-plugin 一致。

    Args:
        keyword: 中文关键词，如 "抱"、"贴"、"亲"、"拍"、"举牌"、"猛亲"、"抱紧" 等
        target_qq: 目标用户的 QQ 号（可从聊天上下文获取）
        text: 部分模板需要的文字参数（如举牌内容），不需要时留空

    Returns:
        str: 执行结果描述

    Example:
        result = await send_meme_command("抱", "123456")
        result = await send_meme_command("举牌", "123456", text="晚安")
    """
    prefix = config.COMMAND_FORCE_PREFIX.strip() or "bq"

    # 防重复: 同一聊天同一关键词在短时间内不重复发送
    dedup_key = _ctx.chat_key
    now = time.time()
    last = _meme_dedup.get(dedup_key)
    if last and last[0] == keyword.strip().lower() and now - last[1] < _MEME_DEDUP_WINDOW:
        logger.info(f"[meme] 跳过重复调用: {keyword} (within {_MEME_DEDUP_WINDOW}s)")
        return f"表情包已发送，无需重复调用"
    _meme_dedup[dedup_key] = (keyword.strip().lower(), now)

    client = _get_client()
    index = await client.get_index()

    kw_lower = keyword.strip().lower()
    matched_summary = None
    for summary in index.values():
        for alias in summary["keywords"] + summary["shortcuts"]:
            if str(alias).strip().lower() == kw_lower:
                matched_summary = summary
                break
        if matched_summary:
            break

    if not matched_summary:
        results = client.search(keyword, 5)
        if results:
            for r in results:
                rkey = r["key"] if isinstance(r, dict) else None
                if rkey and rkey in index:
                    matched_summary = index[rkey]
                    break

    if not matched_summary:
        raise RuntimeError(
            f"未找到关键词 '{keyword}' 对应的模板。请用 search_meme('{keyword}') 搜索可用模板。"
        )

    key = matched_summary["key"]

    target_str = target_qq.strip()

    from nonebot.adapters.onebot.v11 import Message as OBMessage, MessageSegment as OBSeg
    if text:
        cmd_msg = OBMessage(OBSeg.text(f"{prefix}{keyword} {text}"))
    elif target_str:
        cmd_msg = OBMessage(OBSeg.text(f"{prefix}{keyword} ") + OBSeg.at(int(target_str)))
    else:
        cmd_msg = OBMessage(OBSeg.text(f"{prefix}{keyword}"))

    try:
        # Yunzai 风格的 bq 文本只适用于 OneBot/QQ；Web 等适配器直接发图片即可。
        if _ctx.chat_key.startswith("onebot_v11-"):
            await _send_msg_direct(_ctx.chat_key, cmd_msg)
    except Exception as e:
        logger.warning(f"[meme] 发送指令文本失败: {e}")

    user_ids_list: List[str] = []
    if matched_summary["min_images"] >= 2:
        user_ids_list = ["me", target_str] if target_str else ["me"]
    elif matched_summary["min_images"] >= 1:
        user_ids_list = [target_str] if target_str else []

    images = await _resolve_images(_ctx, user_ids_list, [])

    if not images and matched_summary["min_images"] > 0 and _ctx.from_platform_userid:
        try:
            images.insert(0, await client.download_image(_avatar_url(str(_ctx.from_platform_userid))))
        except Exception as e:
            logger.warning(f"[meme] 获取触发者头像失败: {e}")

    if matched_summary["max_images"] > 0 and len(images) > matched_summary["max_images"]:
        images = images[: matched_summary["max_images"]]

    texts_list = [text] if text.strip() else []
    final_texts = _prepare_texts(matched_summary, texts_list)

    try:
        _check_counts(matched_summary, len(images), len(final_texts))
    except RuntimeError as e:
        return f"表情包素材数量不匹配: {e}"

    args_json, notes = _build_args(matched_summary, None, "", "unknown")

    try:
        img_content = await client.generate(key, images, final_texts, args_json)
    except MemeAPIError as e:
        return f"表情包生成失败: {e}"

    try:
        await _send_image_direct(_ctx.chat_key, img_content)
    except Exception as e:
        logger.warning(f"[meme] 直接发送图片失败，回退到 send_image: {e}")
        sandbox_path = await _ctx.fs.mixed_forward_file(
            img_content, file_name=f"meme_{key}_{int(time.time())}.png"
        )
        try:
            await _ctx.send_image(sandbox_path)
        except Exception as e2:
            return f"表情包生成成功但发送失败: {e2}"

    logger.info(f"[meme] Yunzai 风格表情包发送成功: {keyword} -> {key}")
    result_msg = f"已发送 {prefix}{keyword} 表情包 [{key}]"
    if notes:
        result_msg += f"\n注意: {notes}"
    return result_msg


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
