"""meme-generator API 客户端

对接 MemeCrafters/meme-generator 的 HTTP API（移植自 Yunzai meme-plugin 的调用逻辑）：
- GET  /memes/keys        获取全部模板 key
- GET  /memes/{key}/info  获取模板参数定义
- POST /memes/{key}/      multipart 生成表情包（images + texts + args）
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


class MemeAPIError(RuntimeError):
    """meme-generator API 调用错误"""


def _normalize_str_list(value: Any) -> List[str]:
    if not value:
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name") or item.get("text") or item.get("pattern")
            result.append(str(name) if name else json.dumps(item, ensure_ascii=False))
        else:
            result.append(str(item))
    return result


class MemeClient:
    """meme-generator API 客户端（带模板索引缓存）"""

    def __init__(self, base_url: str, timeout: float = 60.0, cache_dir: Optional[Path] = None):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._keys: List[str] = []
        self._keys_at = 0.0
        self._index: Dict[str, Dict[str, Any]] = {}
        self._index_at = 0.0
        self._index_lock = asyncio.Lock()

    # ------------------------------------------------------------------ 基础请求

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise MemeAPIError(f"无法连接 meme-generator 服务 ({url}): {e}") from e

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = resp.text
        try:
            body = resp.json()
            if isinstance(body, dict) and "detail" in body:
                detail = str(body["detail"])
        except Exception:
            pass
        raise MemeAPIError(f"meme-generator API 错误 (HTTP {resp.status_code}): {detail}")

    # ------------------------------------------------------------------ 模板列表 / 索引

    async def list_keys(self) -> List[str]:
        if self._keys and time.monotonic() - self._keys_at < 3600:
            return self._keys
        resp = await self._request("GET", f"{self._base}/memes/keys")
        self._check(resp)
        data = resp.json()
        keys = []
        for item in data:
            keys.append(str(item["key"]) if isinstance(item, dict) else str(item))
        self._keys = keys
        self._keys_at = time.monotonic()
        return keys

    async def get_index(self) -> Dict[str, Dict[str, Any]]:
        """获取全部模板的摘要索引（含关键词、参数要求），带内存与文件缓存"""
        async with self._index_lock:
            if self._index and time.monotonic() - self._index_at < 24 * 3600:
                return self._index

            cached = self._load_index_cache()
            if cached is not None:
                self._index = cached
                self._index_at = time.monotonic()
                return self._index

            keys = await self.list_keys()
            index = await self._build_index(keys)
            self._index = index
            self._index_at = time.monotonic()
            self._save_index_cache(index)
            return self._index

    def peek_index(self) -> Dict[str, Dict[str, Any]]:
        """非阻塞读取内存中的索引（不触发构建，未就绪时为空）"""
        return self._index

    async def _build_index(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        semaphore = asyncio.Semaphore(24)

        async def fetch_one(key: str) -> Optional[tuple[str, Dict[str, Any]]]:
            async with semaphore:
                try:
                    resp = await self._request("GET", f"{self._base}/memes/{key}/info")
                    self._check(resp)
                    return key, resp.json()
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).warning(f"获取模板 {key} 信息失败: {e}")
                    return None

        results = await asyncio.gather(*(fetch_one(key) for key in keys))
        index: Dict[str, Dict[str, Any]] = {}
        for item in results:
            if item is None:
                continue
            key, info = item
            index[key] = self._summarize(key, info)
        return index

    def _summarize(self, key: str, info: Dict[str, Any]) -> Dict[str, Any]:
        params = info.get("params_type") or {}
        args_type = params.get("args_type")
        arg_names: List[str] = []
        arg_desc: Dict[str, str] = {}
        if isinstance(args_type, dict):
            model = args_type.get("args_model") or {}
            for name, prop in (model.get("properties") or {}).items():
                if name == "user_infos":
                    continue
                arg_names.append(name)
                arg_desc[name] = str(prop.get("description") or prop.get("title") or "")
        return {
            "key": key,
            "keywords": _normalize_str_list(info.get("keywords")),
            "shortcuts": _normalize_str_list(info.get("shortcuts")),
            "tags": _normalize_str_list(info.get("tags")),
            "min_images": int(params.get("min_images") or 0),
            "max_images": int(params.get("max_images") or 0),
            "min_texts": int(params.get("min_texts") or 0),
            "max_texts": int(params.get("max_texts") or 0),
            "default_texts": _normalize_str_list(params.get("default_texts")),
            "has_args": args_type is not None,
            "arg_names": arg_names,
            "arg_desc": arg_desc,
            "arg_examples": args_type.get("args_examples") if isinstance(args_type, dict) else None,
        }

    # ------------------------------------------------------------------ 索引缓存文件

    def _cache_file(self) -> Optional[Path]:
        if self._cache_dir is None:
            return None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            return self._cache_dir / "memes_index.json"
        except OSError:
            return None

    def _load_index_cache(self) -> Optional[Dict[str, Dict[str, Any]]]:
        cache_file = self._cache_file()
        if cache_file is None or not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - float(data.get("cached_at", 0)) > 24 * 3600:
                return None
            index = data.get("index")
            return index if isinstance(index, dict) and index else None
        except Exception:
            return None

    def _save_index_cache(self, index: Dict[str, Dict[str, Any]]) -> None:
        cache_file = self._cache_file()
        if cache_file is None:
            return
        try:
            cache_file.write_text(
                json.dumps({"cached_at": time.time(), "index": index}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ 查询 / 生成

    async def get_info(self, key: str) -> Dict[str, Any]:
        resp = await self._request("GET", f"{self._base}/memes/{key}/info")
        self._check(resp)
        return resp.json()

    def search(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """在模板索引中按关键词搜索，返回按匹配度排序的模板摘要

        匹配策略（按优先级）：
        0. 关键词完全命中模板别名
        1. 别名与搜索词互为前缀或包含（如「摸头」vs「想要一个摸头」）
        2. 模板 key 与搜索词互为子串
        3. 中文模糊：搜索词的相邻双字（bigram）在模板文本中的命中
        搜索前会剥离「表情包/表情/图片」等常见后缀，避免整句搜索失准。
        """
        kw = (keyword or "").strip().lower()
        for suffix in ("表情包", "表情", "图片", "模板", "的图", "图"):
            if len(kw) > len(suffix) and kw.endswith(suffix):
                kw = kw[: -len(suffix)].strip()
                break

        def searchable(summary: Dict[str, Any]) -> str:
            return " ".join(
                [summary["key"], *summary["keywords"], *summary["shortcuts"], *summary["tags"]]
            ).lower()

        candidates = list(self._index.values())
        if not kw:
            import random

            random.shuffle(candidates)
            return candidates[:limit]

        scored: List[tuple[int, int, Dict[str, Any]]] = []
        for summary in candidates:
            keywords = [k.lower() for k in summary["keywords"] if k]
            text = searchable(summary)
            weight = 0
            if kw in keywords:
                score = 0
            elif any(kw.startswith(k) or k.startswith(kw) or (len(k) >= 2 and k in kw) for k in keywords):
                score = 1
            elif kw in summary["key"].lower() or summary["key"].lower() in kw:
                score = 2
            else:
                grams = [kw[i: i + 2] for i in range(len(kw) - 1)] if len(kw) >= 2 else [kw]
                weight = sum(1 for g in grams if g in text)
                if not weight:
                    continue
                score = 3
            scored.append((score, weight, summary))

        scored.sort(key=lambda item: (item[0], -item[1]))
        return [summary for _, _, summary in scored[:limit]]

    async def generate(
        self,
        key: str,
        images: List[bytes],
        texts: List[str],
        args_json: Optional[str] = None,
    ) -> bytes:
        """调用 meme-generator 生成表情包，返回图片二进制数据"""
        files = []
        for i, image in enumerate(images):
            files.append(("images", (f"image{i}.png", image, "image/png")))
        data: List[tuple[str, str]] = [("texts", text) for text in texts if text]
        if args_json is not None:
            data.append(("args", args_json))

        kwargs: Dict[str, Any] = {}
        if files:
            kwargs["files"] = files
        if data:
            kwargs["data"] = data

        resp = await self._request("POST", f"{self._base}/memes/{key}/", **kwargs)
        self._check(resp)
        content = resp.content
        if not content:
            raise MemeAPIError(f"meme-generator 返回了空数据 (key={key})")
        return content

    async def download_image(self, url: str) -> bytes:
        """下载图片（如 QQ 头像），返回二进制数据"""
        resp = await self._request("GET", url)
        self._check(resp)
        return resp.content
