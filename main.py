"""明日方舟公招助手 —— 明日方舟公招词条分析与图片识别插件。

指令：
    /公开招募 [词条...]   发送公招截图或直接输入词条，列出 0~3 词条组合的星级收益
    /公招干员 词条组合    查看某个词条组合的全部候选干员
    /公招词条             列出全部公招词条
    /公招帮助             指令说明

识别方式：用多模态模型读图（当前对话模型、配置里指定的 provider_id，
或在配置里填一条独立的 OpenAI 兼容识图接口）；模型不支持视觉时，
直接把词条打在指令后面即可。
支持一次发多张截图（分条发也行，指令先发时会在几秒内等图），逐张识别后
默认以合并转发（聊天记录）发送结果。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

try:  # 合并转发组件（部分老版本/平台没有）
    from astrbot.api.message_components import Node, Nodes
except ImportError:  # pragma: no cover - 取决于 AstrBot 版本
    Node = None  # type: ignore[assignment]
    Nodes = None  # type: ignore[assignment]

try:  # 插件以包形式加载用相对导入；单文件加载时回落到同目录导入
    from .recruit_core import (
        DEFAULT_STAR_FLOOR,
        RecruitDatabase,
        extract_tags,
        load_database,
        render_pool,
        render_report,
        render_star_pool,
        render_tag_catalog,
    )
except ImportError:  # pragma: no cover - 取决于 AstrBot 的加载方式
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from recruit_core import (  # type: ignore[no-redef]
        DEFAULT_STAR_FLOOR,
        RecruitDatabase,
        extract_tags,
        load_database,
        render_pool,
        render_report,
        render_star_pool,
        render_tag_catalog,
    )

__version__ = "v1.0.0"

DEFAULT_CONFIG: dict = {
    "recognize_mode": "auto",
    "provider_id": "",
    # 自定义识图接口（OpenAI 兼容）：开启后完全绕过 bot 里配置的模型
    "custom_vision_enabled": False,
    "custom_vision_base_url": "",
    "custom_vision_model": "",
    "custom_vision_api_key": "",
    "custom_vision_timeout": 60,
    "star_floor": DEFAULT_STAR_FLOOR,
    "max_tags": 3,
    "max_per_tier": 6,
    "chunk_size": 1200,
    "image_cache_ttl": 180,
    "image_wait": 8,
    "max_images": 5,
    "forward_result": True,
}

WAKE_PREFIXES = ("/", "!", "！", "#", "。", "／")

# 指令名（长名在前），用于判断指令后面是否还跟了内容
# 「公开招募小助手」是旧名字，保留作别名，避免老用户改口
RECRUIT_NAMES = (
    "明日方舟公招助手",
    "公开招募小助手",
    "公开招募",
    "公招分析",
    "公招助手",
    "公招",
)

# 老版本 AstrBot 可能没有 EventMessageType，缺失时跳过「最近截图」缓存
_ALL_MESSAGES = getattr(getattr(filter, "EventMessageType", None), "ALL", None)

# 「先发截图 → 再发 /公招」的截图缓存上限（会话数）
IMAGE_CACHE_MAX_SESSIONS = 20

# 合并转发单个节点的内容上限（超出拆成多个节点）
NODE_TEXT_LIMIT = 3000

# 合并转发节点的显示名
NODE_NAME = "明日方舟公招助手"

# 合并转发的最后一条（兜底声明）
FORWARD_FOOTER = "招募建议仅供参考，博士们请根据自身 BOX 灵活调整呦~"

# 等到第一张截图后再等多久（期间没新图就开识别）
WAIT_QUIET_SECONDS = 3.0

# 多张截图并行识别的上限：再高容易撞上模型侧的限流（429），而且耗时收益不明显
RECOGNIZE_CONCURRENCY = 3

# 自定义识图接口用到的图片 MIME（本地图会转成 data URL 发过去）
IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


def _image_mime(raw: bytes, suffix: str) -> str:
    """先看文件头再退到扩展名：平台给的临时文件常常没有后缀。"""

    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return IMAGE_MIME.get(suffix, "image/png")


# 让模型只输出图中词条，避免它自由发挥
RECOGNIZE_PROMPT = """你是《明日方舟》公开招募界面的识别助手。
图片是游戏内「公开招募」界面截图，中间一栏有 5 个招募词条按钮，
其中黄色字体的「资深干员」「高级资深干员」也是词条。

请只在图中读出这 5 个词条，一行一个，不要编号、不要解释、不要输出其它文字。
词条只可能取以下值：
职业：近卫 狙击 重装 医疗 辅助 术师 特种 先锋
位置：近战位 远程位
特性：控场 爆发 治疗 支援 费用回复 输出 生存 群攻 防护 减速 削弱 快速复活 位移 召唤 支援机械 元素
资历：新手 资深干员 高级资深干员

如果某个词条看不清，请输出最接近的上面某个词条；完全无法判断就不要输出。"""

HELP_TEXT = """明日方舟公招助手
————————————
/公开招募            # 直接带图 / 先发 1~N 张截图再发指令 / 先发指令再贴图
/公开招募 输出 生存   # 直接输入词条，空格/逗号分隔均可
/公招干员 输出+生存    # 查看该组合的全部候选干员
/公招词条            # 全部公招词条一览
/公招帮助            # 本说明

说明：每张截图单独识别，结果默认以合并转发（聊天记录）发送，
平台不支持合并转发时（如 QQ 官方机器人）自动改为普通文本。
先发指令时会等几秒接收截图，截图缓存几分钟内有效。
每次公招最多选 3 个词条，助手会枚举 1~3 词条的全部组合，
按「必出6★ → 必出5★ → 保底4★+极小概率5★ → 保底4★ → 小概率5★ → 小概率4★」的顺序给出推荐。"""


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if value is None:
        return default
    return bool(value)


# 合并转发（聊天记录）只有 OneBot 系适配器支持。其他平台（如 QQ 官方机器人）
# 会把 Nodes 组件静默丢掉：AstrBot 只打一条 debug 日志、send() 照样返回成功，
# 用户这边什么都收不到，所以这些平台必须降级成普通文本
FORWARD_PLATFORMS = ("aiocqhttp",)


def supports_forward(event) -> bool:
    """当前平台是否支持合并转发。拿不到平台名时按不支持处理。"""

    for attr in ("get_platform_name", "get_platform_id"):
        getter = getattr(event, attr, None)
        if not callable(getter):
            continue
        try:
            name = str(getter() or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if name:
            return any(key in name for key in FORWARD_PLATFORMS)
    return False


def chunk_text(text: str, limit: int) -> list[str]:
    """按行切分长文本，避免超出平台单条消息长度。"""

    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current = f"{current}\n{line}"
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


class ArknightsRecruitPlugin(Star):
    """明日方舟公招助手：识别公招词条 → 枚举组合 → 推荐保底星级。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

        cfg = {**DEFAULT_CONFIG, **dict(config or {})}
        self.cfg = cfg
        # 旧版本配置里的 llm / ocr 都当 auto 处理：读图统一走多模态模型
        self.recognize_mode = str(cfg.get("recognize_mode") or "auto").strip().lower()
        self.provider_id = str(cfg.get("provider_id") or "").strip()
        # 自定义识图接口：地址 / 模型 / 秘钥三项都填了才算生效，否则走 bot 的模型
        self.custom_vision_enabled = _as_bool(cfg.get("custom_vision_enabled"), False)
        self.custom_vision_base_url = str(cfg.get("custom_vision_base_url") or "").strip()
        self.custom_vision_model = str(cfg.get("custom_vision_model") or "").strip()
        self.custom_vision_api_key = str(cfg.get("custom_vision_api_key") or "").strip()
        self.custom_vision_timeout = max(5, _as_int(cfg.get("custom_vision_timeout"), 60))
        self.star_floor = max(1, min(5, _as_int(cfg.get("star_floor"), DEFAULT_STAR_FLOOR)))
        self.max_tags = max(1, min(5, _as_int(cfg.get("max_tags"), 3)))
        self.max_per_tier = max(1, _as_int(cfg.get("max_per_tier"), 6))
        self.chunk_size = max(200, _as_int(cfg.get("chunk_size"), 1200))
        self.image_cache_ttl = max(0, _as_int(cfg.get("image_cache_ttl"), 180))
        self.image_wait = max(0, min(60, _as_int(cfg.get("image_wait"), 8)))
        self.max_images = max(1, min(9, _as_int(cfg.get("max_images"), 5)))
        self.forward_result = _as_bool(cfg.get("forward_result"), True)
        # 会话 → [(记录时间, 图片组件)]，支持「多张截图分别发送 / 一条消息多图」
        self._image_cache: dict[str, list[tuple[float, object]]] = {}
        # /公招 发出后短暂等待截图：会话 → 开始等待的时间（秒）
        self._waiting: dict[str, float] = {}
        # 等待期间用户补发的文本（用于提前结束等待）
        self._waited_text: dict[str, tuple[float, str]] = {}

        self._data_error = ""
        try:
            self.db: RecruitDatabase | None = load_database()
        except Exception as exc:  # noqa: BLE001
            self.db = None
            self._data_error = str(exc)
            logger.error("[公招] 干员数据加载失败：%s", exc, exc_info=True)
        else:
            logger.info(
                "[公招] 干员数据已加载：%s 名干员（数据日期 %s）",
                len(self.db.operators),
                self.db.generated_at or "未知",
            )

    # ------------------------------------------------------------------
    # 最近截图缓存（先发图、再发 /公招）
    # ------------------------------------------------------------------
    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        """会话标识：同一会话内才复用缓存图片。"""

        key = getattr(event, "unified_msg_origin", "") or ""
        if not key:
            obj = getattr(event, "message_obj", None)
            key = str(
                getattr(obj, "session_id", "")
                or getattr(getattr(obj, "sender", None), "user_id", "")
                or "default"
            )
        return str(key)

    if _ALL_MESSAGES is not None:  # pragma: no cover - 依赖运行环境

        @filter.event_message_type(_ALL_MESSAGES)
        async def cache_recent_images(self, event: AstrMessageEvent):
            """累记消息里的图片，供之后的 /公招 使用（不回复、不拦截）。"""

            if getattr(event, "_recruit_consumed", False):
                return
            key = self._session_key(event)
            images = self._iter_images(event)

            # 等待截图期间，用户补发文字词条也能提前结束等待
            if not images and key in self._waiting:
                text = self._raw_text(event)
                if text and "公招" not in text and "公开招募" not in text:
                    self._waited_text[key] = (time.monotonic(), text)

            ttl = self.image_cache_ttl
            if ttl <= 0 or not images:
                return

            now = time.monotonic()
            for other in list(self._image_cache):
                self._image_cache[other] = [
                    item for item in self._image_cache[other] if now - item[0] <= ttl
                ]
                if not self._image_cache[other]:
                    self._image_cache.pop(other, None)

            bucket = self._image_cache.setdefault(key, [])
            known = {id(comp) for _, comp in bucket}
            for comp in images:
                if id(comp) not in known:
                    bucket.append((now, comp))
            # 只保留最近的 max_images 张，避免刷图把内存堆满
            if len(bucket) > self.max_images:
                del bucket[: len(bucket) - self.max_images]

            while len(self._image_cache) > IMAGE_CACHE_MAX_SESSIONS:
                oldest = min(
                    self._image_cache,
                    key=lambda k: self._image_cache[k][-1][0] if self._image_cache[k] else 0,
                )
                self._image_cache.pop(oldest, None)

    def _recent_images(self, event: AstrMessageEvent) -> list:
        """取出该会话最近发送的截图（旧→新，用掉即清除，避免重复识别）。"""

        if self.image_cache_ttl <= 0:
            return []
        items = self._image_cache.pop(self._session_key(event), [])
        now = time.monotonic()
        return [comp for saved_at, comp in items if now - saved_at <= self.image_cache_ttl]

    async def _wait_for_images(self, event: AstrMessageEvent) -> list:
        """指令先到、图后到：短暂等待截图，期间有新图就续一段静默期。"""

        key = self._session_key(event)
        started = time.monotonic()
        deadline = started + self.image_wait
        count = 0
        self._waiting[key] = started
        self._waited_text.pop(key, None)
        try:
            while True:
                now = time.monotonic()
                if len(self._image_cache.get(key, [])) != count:
                    count = len(self._image_cache.get(key, []))
                    deadline = max(deadline, now + WAIT_QUIET_SECONDS)
                text = self._waited_text.get(key)
                if text and text[0] > started:
                    break
                if now >= deadline:
                    break
                await asyncio.sleep(0.3)
        finally:
            self._waiting.pop(key, None)
        return self._recent_images(event)

    # ------------------------------------------------------------------
    # 参数与消息工具
    # ------------------------------------------------------------------
    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        try:
            raw = event.get_message_str() or ""
        except Exception:  # noqa: BLE001
            raw = getattr(event, "message_str", "") or ""
        return re.sub(r"\s+", " ", raw.strip())

    @classmethod
    def _arg_text(cls, event: AstrMessageEvent) -> str:
        """取指令之后的参数文本（去掉唤醒前缀与指令名）。"""

        text = cls._raw_text(event)
        for prefix in WAKE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        _, _, rest = text.partition(" ")
        return rest.strip()

    @classmethod
    def _inline_tail(cls, event: AstrMessageEvent) -> str:
        """指令名后面紧跟的剩余内容（无空格写法用）。"""

        text = cls._raw_text(event)
        for prefix in WAKE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        for name in RECRUIT_NAMES:
            if text.startswith(name):
                return text[len(name) :].strip()
        return ""

    async def _send_text(self, event: AstrMessageEvent, text: str) -> None:
        for chunk in chunk_text(text, self.chunk_size):
            await event.send(MessageChain([Plain(chunk)]))

    @staticmethod
    def _iter_images(event: AstrMessageEvent) -> list:
        """取出消息里的图片组件。"""

        try:
            messages = event.get_messages() or []
        except Exception:  # noqa: BLE001
            messages = getattr(getattr(event, "message_obj", None), "message", []) or []
        images = []
        for comp in messages:
            class_name = type(comp).__name__.lower()
            comp_type = str(getattr(comp, "type", "")).lower()
            if class_name == "image" or "image" in class_name or "image" in comp_type:
                images.append(comp)
        return images

    # ------------------------------------------------------------------
    # 图片落地（供模型读图使用）
    # ------------------------------------------------------------------
    @staticmethod
    def _temp_path(suffix: str = ".jpg") -> Path:
        folder = Path(tempfile.gettempdir()) / "astrbot_recruit"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{uuid.uuid4().hex}{suffix}"

    async def _image_local_path(self, comp) -> Path | None:
        """把图片组件转成本地文件路径（已存在的直接用，网络图下载到临时目录）。"""

        path = str(getattr(comp, "path", "") or "")
        if path and Path(path).is_file():
            return Path(path)

        file_field = str(getattr(comp, "file", "") or "")
        if file_field.startswith("file://"):
            candidate = Path(file_field[7:])
            if candidate.is_file():
                return candidate
        elif file_field and Path(file_field).is_file():
            return Path(file_field)

        if file_field.startswith("base64://"):
            try:
                raw = base64.b64decode(file_field[9:])
            except Exception:  # noqa: BLE001
                raw = b""
            if raw:
                target = self._temp_path()
                target.write_bytes(raw)
                return target

        converter = getattr(comp, "convert_to_file_path", None)
        if callable(converter):
            try:
                result = converter()
                if asyncio.iscoroutine(result):
                    result = await result
                if result and Path(str(result)).is_file():
                    return Path(str(result))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[公招] convert_to_file_path 失败：%s", exc)

        url = str(getattr(comp, "url", "") or "")
        if url.startswith(("http://", "https://")):
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=20) as resp:
                        if resp.status == 200:
                            target = self._temp_path()
                            target.write_bytes(await resp.read())
                            return target
            except Exception as exc:  # noqa: BLE001
                logger.warning("[公招] 下载图片失败：%s", exc)
        return None

    async def _image_ref_for_model(self, comp) -> str | None:
        """给多模态模型的图片引用：优先 http 链接，其次本地路径。"""

        url = str(getattr(comp, "url", "") or "")
        if url.startswith(("http://", "https://")):
            return url
        path = await self._image_local_path(comp)
        return str(path) if path else None

    # ------------------------------------------------------------------
    # 词条识别
    # ------------------------------------------------------------------
    def _custom_vision_ready(self) -> bool:
        """自定义识图接口是否可用：开关打开且地址 / 模型 / 秘钥都填了。"""

        if not self.custom_vision_enabled:
            return False
        missing = [
            name
            for name, value in (
                ("custom_vision_base_url", self.custom_vision_base_url),
                ("custom_vision_model", self.custom_vision_model),
                ("custom_vision_api_key", self.custom_vision_api_key),
            )
            if not value
        ]
        if missing:
            logger.warning(
                "[公招] 自定义识图已开启但缺配置：%s，回退到 bot 的对话模型",
                "、".join(missing),
            )
            return False
        return True

    def _vision_endpoint(self) -> str:
        """拼出 chat/completions 地址，容忍用户少写 /v1 或多写全路径。"""

        base = self.custom_vision_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        # 已经带了版本段（/v1、/v3 之类）就直接续 chat/completions，不加 /v1
        if re.search(r"/v\d+$", base):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    async def _vision_image_ref(self, comp) -> str | None:
        """给自定义接口的图片引用：http 链接原样传，本地图转 data URL。"""

        url = str(getattr(comp, "url", "") or "")
        if url.startswith(("http://", "https://")):
            return url
        path = await self._image_local_path(comp)
        if not path:
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("[公招] 读取截图失败：%s", exc)
            return None
        suffix = path.suffix.lower().lstrip(".")
        mime = _image_mime(raw, suffix)
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    async def _recognize_by_custom_api(self, comps: list) -> list[str]:
        """直连自定义识图接口（OpenAI 兼容），完全绕过 bot 里配置的模型。"""

        images = [ref for ref in [await self._vision_image_ref(c) for c in comps] if ref]
        if not images:
            raise RuntimeError("拿不到图片内容，请直接把词条打在指令后面")

        payload = {
            "model": self.custom_vision_model,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": RECOGNIZE_PROMPT},
                        *(
                            {"type": "image_url", "image_url": {"url": url}}
                            for url in images
                        ),
                    ],
                }
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.custom_vision_api_key}",
            "Content-Type": "application/json",
        }
        endpoint = self._vision_endpoint()

        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - AstrBot 自带
            raise RuntimeError("缺少 aiohttp，无法请求自定义识图接口") from exc

        timeout = aiohttp.ClientTimeout(total=self.custom_vision_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as resp:
                    status = resp.status
                    body = await resp.text()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"自定义识图接口请求失败：{exc}") from exc
        if status >= 400:
            raise RuntimeError(f"自定义识图接口返回 HTTP {status}：{body[:200]}")

        try:
            content = json.loads(body)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"自定义识图接口返回无法解析：{body[:200]}") from exc
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        tags = extract_tags(str(content or ""), self.db.all_tags if self.db else None)
        if not tags:
            raise RuntimeError("自定义识图模型没有读出词条")
        return tags

    def _get_provider(self):
        if self.provider_id:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(self.provider_id)
                    if provider is not None:
                        return provider
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[公招] 指定模型 %s 不可用：%s", self.provider_id, exc)
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            try:
                return getter()
            except TypeError:
                return getter(None)
        return None

    async def _recognize_by_llm(self, refs: list[str]) -> list[str]:
        provider = self._get_provider()
        if provider is None:
            raise RuntimeError("没有可用的对话模型")
        last_error: Exception | None = None
        for kwargs in (
            {"prompt": RECOGNIZE_PROMPT, "contexts": [], "image_urls": refs},
            {"prompt": RECOGNIZE_PROMPT, "image_urls": refs},
        ):
            try:
                resp = await provider.text_chat(**kwargs)
            except TypeError as exc:  # 不同版本签名差异
                last_error = exc
                continue
            text = (
                getattr(resp, "completion_text", "")
                or getattr(resp, "text", "")
                or str(resp or "")
            )
            tags = extract_tags(text, self.db.all_tags if self.db else None)
            if tags:
                return tags
            raise RuntimeError("模型没有读出词条")
        raise RuntimeError(f"模型调用失败：{last_error}")

    async def _recognize_images(self, comps: list) -> list[str]:
        """用多模态模型识别图片里的公招词条（开了自定义接口就走自定义）。"""

        if self.recognize_mode == "off":
            raise RuntimeError("图片识别已在插件配置中关闭，请直接输入词条")

        custom = self._custom_vision_ready()
        try:
            if custom:
                return await self._recognize_by_custom_api(comps)
            refs = [ref for ref in [await self._image_ref_for_model(c) for c in comps] if ref]
            if not refs:
                raise RuntimeError("拿不到图片内容，请直接把词条打在指令后面")
            tags = await self._recognize_by_llm(refs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[公招] 模型识别失败：%s", exc)
            hint = (
                "请检查自定义识图接口的地址 / 模型 / 秘钥"
                if custom
                else "请确认模型支持图片"
            )
            raise RuntimeError(f"模型读图失败（{exc}）。{hint}，或直接输入词条") from exc
        if not tags:
            raise RuntimeError("模型没有读出词条，请直接输入词条")
        return tags

    async def _recognize_batch(self, comps: list) -> list[dict]:
        """逐张识别，每张独立返回（失败的只影响自己）；多张时并发跑。"""

        semaphore = asyncio.Semaphore(RECOGNIZE_CONCURRENCY)

        async def _one(index: int, comp) -> dict:
            async with semaphore:
                try:
                    tags = await self._recognize_images([comp])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[公招] 第 %s 张截图识别失败：%s", index, exc)
                    result = {"index": index, "tags": [], "error": str(exc)}
                else:
                    result = {"index": index, "tags": tags, "error": None}
            logger.info(
                "[公招] 第 %s/%s 张截图识别完成：%s",
                index,
                len(comps),
                "、".join(result["tags"]) or result["error"],
            )
            return result

        # gather 按传入顺序返回，并发只影响耗时，不影响结果的顺序
        return list(
            await asyncio.gather(*(_one(index, comp) for index, comp in enumerate(comps, 1)))
        )

    async def _deliver_results(
        self, event: AstrMessageEvent, db: RecruitDatabase, results: list[dict]
    ) -> None:
        """把逐张识别结果打包发送（默认合并转发，不可用时回退文本）。"""

        total = len(results)
        if not any(r["tags"] for r in results):
            detail = "；".join(f"第 {r['index']} 张：{r['error']}" for r in results)
            await self._send_text(
                event,
                f"没有识别到词条（{detail}）。\n"
                "可以直接输入词条，例如：/公招 输出 生存 近战位",
            )
            return

        entries: list[dict] = []
        pools: list[str] = []
        for r in results:
            label = f"第 {r['index']}/{total} 张截图" if total > 1 else ""
            if r["tags"]:
                text = render_report(
                    db,
                    r["tags"][:5],
                    max_tags=self.max_tags,
                    max_per_tier=self.max_per_tier,
                    star_floor=self.star_floor,
                    label=label,
                )
                summary = " / ".join(r["tags"][:5])
                # 只勾中一个资历词条时，池名单单独成一条消息
                pool = render_star_pool(db, r["tags"][:5], star_floor=self.star_floor)
                if pool:
                    pools.append(pool)
            else:
                text = f"{label}\n识别失败：{r['error']}" if label else f"识别失败：{r['error']}"
                summary = "识别失败"
            entries.append({"index": r["index"], "text": text, "summary": summary})

        forward_ok = self.forward_result and Node is not None and Nodes is not None
        if forward_ok and supports_forward(event):
            if await self._send_forward(event, entries, pools):
                return
        # 文本回退：免责声明并到最后一条，少发一条（QQ 官方机器人被动回复有次数上限）
        messages = [entry["text"] for entry in entries] + pools
        messages[-1] = f"{messages[-1]}\n\n{FORWARD_FOOTER}"
        for message in messages:
            await self._send_text(event, message)

    async def _send_forward(
        self, event: AstrMessageEvent, entries: list[dict], pools: list[str]
    ) -> bool:
        """用合并转发发送识别结果，平台/版本不支持时返回 False。"""

        nodes = []
        if len(entries) > 1:
            lines = [f"本次共识别 {len(entries)} 张公招截图", ""]
            lines += [f"第 {e['index']} 张：{e['summary']}" for e in entries]
            nodes.extend(self._make_nodes("\n".join(lines)))
        for entry in entries:
            nodes.extend(self._make_nodes(entry["text"]))
        for pool in pools:
            nodes.extend(self._make_nodes(pool))
        nodes.extend(self._make_nodes(FORWARD_FOOTER))

        try:
            builder = getattr(event, "chain_result", None)
            chain = (
                builder([Nodes(nodes)])
                if callable(builder)
                else MessageChain([Nodes(nodes)])
            )
            await event.send(chain)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[公招] 合并转发发送失败，回退为普通文本：%s", exc)
            return False

    @staticmethod
    def _make_nodes(text: str) -> list:
        """长文本按节点上限拆成多个转发节点。"""

        return [
            Node(content=[Plain(part)], name=NODE_NAME, uin="0")
            for part in chunk_text(text, NODE_TEXT_LIMIT)
        ]

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------
    @filter.command(
        "公开招募",
        alias={"公招", "公招分析", "公招助手", "公开招募小助手", "明日方舟公招助手"},
    )
    async def cmd_recruit(self, event: AstrMessageEvent):
        """/公开招募 [词条...] —— 分析公招词条组合（支持多张截图，结果合并转发）。"""

        db = self.db
        if db is None:
            await self._send_text(
                event, f"公招数据未加载，请检查插件 data 目录（{self._data_error}）"
            )
            return

        raw_arg = self._arg_text(event)
        if raw_arg in ("帮助", "help", "?", "？"):
            await self._send_text(event, HELP_TEXT)
            return

        tags = extract_tags(raw_arg, db.all_tags)

        images = self._iter_images(event)
        if not tags:
            cached = self._recent_images(event)
            if cached:
                if images:
                    seen = {id(comp) for comp in images}
                    images = list(images) + [c for c in cached if id(c) not in seen]
                else:
                    images = cached
        if images:
            # 标记已消费，避免全量消息监听把同一条指令里的图又缓存一次
            setattr(event, "_recruit_consumed", True)
        elif (
            not tags
            and not raw_arg
            and not self._inline_tail(event)
            and self.image_wait > 0
            and self.image_cache_ttl > 0
        ):
            # 指令先到、图后到：等一会儿让用户把截图发上来
            await self._send_text(
                event,
                f"已收到指令，请在 {self.image_wait} 秒内发送公招截图（也可直接回复词条）…",
            )
            images = await self._wait_for_images(event)
            if images:
                setattr(event, "_recruit_consumed", True)
            else:
                extra = self._waited_text.pop(self._session_key(event), None)
                if extra:
                    tags = extract_tags(extra[1], db.all_tags)

        if images and not tags:
            images = images[: self.max_images]
            await self._send_text(event, f"正在识别 {len(images)} 张截图中的词条…")
            results = await self._recognize_batch(images)
            await self._deliver_results(event, db, results)
            return

        if not tags and not raw_arg:
            # 兼容「/公招输出生存」这种没有空格的写法
            tags = extract_tags(self._raw_text(event), db.all_tags)

        if not tags:
            await self._send_text(
                event,
                "没有识别到词条。\n"
                "用法：/公招 输出 生存 近战位\n"
                f"{self._cache_hint()}\n"
                "发送 /公招词条 查看全部词条。",
            )
            return

        tags = tags[:5]
        report = render_report(
            db,
            tags,
            max_tags=self.max_tags,
            max_per_tier=self.max_per_tier,
            star_floor=self.star_floor,
        )
        await self._send_text(event, report)
        # 只勾中一个资历词条时，池名单单独成一条消息
        pool = render_star_pool(db, tags, star_floor=self.star_floor)
        if pool:
            await self._send_text(event, pool)

    @filter.command("公招干员", alias={"公开招募干员", "公招池"})
    async def cmd_pool(self, event: AstrMessageEvent):
        """/公招干员 词条组合 —— 查看该组合的全部候选干员。"""

        db = self.db
        if db is None:
            await self._send_text(event, f"公招数据未加载：{self._data_error}")
            return

        raw_arg = self._arg_text(event)
        tags = extract_tags(raw_arg, db.all_tags)
        if not tags:
            await self._send_text(
                event, "请给出词条，例如：/公招干员 输出+生存\n（发送 /公招词条 查看全部词条）"
            )
            return

        combo = db.evaluate(tags, star_floor=self.star_floor)
        if combo is None:
            await self._send_text(
                event,
                f"「{'+'.join(tags)}」没有匹配的候选干员"
                f"（{self.star_floor}★ 起算，词条可能互相矛盾）。",
            )
            return
        await self._send_text(event, render_pool(combo))

    @filter.command("公招词条", alias={"公开招募词条", "公招标签"})
    async def cmd_tags(self, event: AstrMessageEvent):
        """/公招词条 —— 列出全部公招词条。"""

        db = self.db
        groups = db.tag_groups if db else None
        await self._send_text(event, render_tag_catalog(groups))

    @filter.command("公招帮助", alias={"公开招募帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """/公招帮助 —— 指令说明。"""

        await self._send_text(event, HELP_TEXT)

    def _cache_hint(self) -> str:
        """描述「先发截图再发指令」的可用时间。"""

        ttl = self.image_cache_ttl
        if ttl <= 0:
            return "或把公招截图和 /公招 放在同一条消息里（截图缓存已关闭）"
        span = f"{ttl // 60} 分钟内有效" if ttl % 60 == 0 else f"{ttl} 秒内有效"
        return f"或先发送公招截图再发 /公招（{span}）"
