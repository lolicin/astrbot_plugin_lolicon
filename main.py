import os
import re
import io
import time
import uuid
import asyncio
import logging
import aiofiles
import aiohttp
import random
from typing import List, Optional
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import event_message_type, EventMessageType
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger(__name__)

file_lock = asyncio.Lock()


# ==================== 数量解析 ====================

CHINESE_NUMS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 匹配 "3份" "三张" "十个" 等
COUNT_PATTERN = re.compile(r"([0-9]+|[一二三四五六七八九十]+)\s*(?:份|个|张|片)")


def parse_count(count_str: str) -> int:
    """解析数量字符串，支持中文数字。无效返回 -1。"""
    if not count_str:
        return 1
    try:
        return int(count_str)
    except ValueError:
        pass
    if count_str in CHINESE_NUMS:
        return CHINESE_NUMS[count_str]
    if count_str.startswith("十"):
        if len(count_str) == 1:
            return 10
        try:
            return 10 + int(count_str[1])
        except ValueError:
            return 10
    return -1


def extract_count_and_tags(text: str, trigger_words: list) -> tuple:
    """从消息文本提取数量字符串和标签列表。"""
    cleaned = text
    for w in trigger_words:
        cleaned = cleaned.replace(w, " ")
    count_str = ""
    m = COUNT_PATTERN.search(cleaned)
    if m:
        count_str = m.group(1)
        cleaned = cleaned.replace(m.group(0), " ")
    # 去掉色/涩/瑟/图等无意义字
    for ch in "色涩瑟图福利塞":
        cleaned = cleaned.replace(ch, " ")
    tags = [t.strip() for t in re.split(r"[\s,，、]+", cleaned) if t.strip()]
    return count_str, tags


# 缓存文件名形如 "{pid}_p{page}.{ext}"（lolicon 源）；nyan 源为随机 uuid 文件名，不含 pid
PID_PATTERN = re.compile(r"^(\d+)_p\d+\.")


def extract_pid(filename: str) -> Optional[str]:
    """从缓存文件名解析 pid，无 pid 的来源返回 None。"""
    m = PID_PATTERN.match(os.path.basename(filename))
    return m.group(1) if m else None


def collect_pids(filenames: list) -> list:
    """按发送顺序提取 pid 并去重（同一作品的多页 pid 相同）。"""
    pids = []
    for name in filenames:
        pid = extract_pid(name)
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def parse_alias_map(alias_str: str) -> dict:
    """解析别名配置 '白丝=white_pantyhose,萝莉=loli'。"""
    result = {}
    if not alias_str:
        return result
    for pair in re.split(r"[,\n，]+", alias_str):
        if "=" in pair:
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                result[k] = v
    return result


def resolve_tags(tags: list, alias_map: dict) -> list:
    """标签别名映射。"""
    return [alias_map.get(t, t) for t in tags if t]


# ==================== 图片压缩 ====================

_QUALITY_LADDER = (85, 70, 55, 40)
_DIMENSION_LADDER = (2560, 1920, 1280)


def _try_import_pil():
    try:
        from PIL import Image
        return Image
    except Exception:
        return None


def compress_image(data: bytes, max_bytes: int) -> bytes:
    """压缩图片到 max_bytes 以内。Pillow 软依赖，不可用或失败返回原图。"""
    if max_bytes <= 0 or len(data) <= max_bytes:
        return data
    pil = _try_import_pil()
    if pil is None:
        logger.debug("[compress] Pillow unavailable, keep original")
        return data
    try:
        with pil.open(io.BytesIO(data)) as image:
            image.load()
            rgb = image.convert("RGB")
    except Exception as exc:
        logger.warning(f"[compress] decode failed: {exc}")
        return data
    best = data
    orig_w, orig_h = rgb.size
    for max_edge in (None, *_DIMENSION_LADDER):
        candidate = rgb
        if max_edge is not None:
            longest = max(orig_w, orig_h)
            if longest <= max_edge:
                continue
            scale = max_edge / longest
            new_size = (max(1, int(orig_w * scale)), max(1, int(orig_h * scale)))
            try:
                candidate = rgb.resize(new_size, pil.LANCZOS)
            except Exception:
                continue
        for quality in _QUALITY_LADDER:
            try:
                buf = io.BytesIO()
                candidate.save(buf, format="JPEG", quality=quality, optimize=True)
            except Exception:
                continue
            encoded = buf.getvalue()
            if len(encoded) < len(best):
                best = encoded
            if len(encoded) <= max_bytes:
                logger.debug(f"[compress] {len(data)} -> {len(encoded)} (q={quality}, edge={max_edge or 'orig'})")
                return encoded
    logger.debug(f"[compress] could not reach budget {max_bytes}, best={len(best)}")
    return best


async def compress_image_async(data: bytes, max_bytes: int) -> bytes:
    return await asyncio.to_thread(compress_image, data, max_bytes)


# ==================== 速率限制 ====================

class RateLimiter:
    """按用户加锁防并发刷，带 TTL 防泄漏。"""
    MAX_LOCKS = 1000
    LOCK_TTL = 120

    def __init__(self):
        self._locks: dict = {}
        self._lock_times: dict = {}

    async def acquire(self, user_id: str) -> bool:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            t = self._lock_times.get(user_id, 0)
            if time.monotonic() - t > self.LOCK_TTL:
                try:
                    lock.release()
                except RuntimeError:
                    pass
        if lock.locked():
            return False
        await lock.acquire()
        self._lock_times[user_id] = time.monotonic()
        return True

    async def release(self, user_id: str):
        if user_id in self._locks:
            self._locks[user_id].release()
            self._lock_times.pop(user_id, None)
        if len(self._locks) > self.MAX_LOCKS:
            stale = [k for k, v in self._locks.items() if not v.locked()]
            for k in stale[: len(stale) // 2]:
                del self._locks[k]
                self._lock_times.pop(k, None)


_rate_limiter = RateLimiter()


# ==================== 图片管理 ====================

class ImageManager:
    def __init__(self, config: AstrBotConfig):
        self.config = config
        data_path = Path(get_astrbot_data_path())
        self.imgs_folder = str(data_path / "plugin_data" / "astrbot_plugin_lolicon" / "imgs")
        self.supported_extensions = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
        self.max_file_size = 20 * 1024 * 1024

        self._download_task = None
        self._refill_running = False
        self.download_lock = asyncio.Lock()

        self._init_folder()

    @property
    def cache_size(self) -> int:
        return int(self.config.get("cache_size", 10))

    @property
    def refill_threshold(self) -> int:
        return int(self.config.get("refill_threshold", 5))

    @property
    def refill_interval(self) -> int:
        return int(self.config.get("refill_interval", 300))

    def _init_folder(self):
        os.makedirs(self.imgs_folder, exist_ok=True)

    async def get_image_list(self):
        async with file_lock:
            try:
                files = await asyncio.to_thread(os.listdir, self.imgs_folder)
                return [f for f in files if os.path.splitext(f)[1].lower() in self.supported_extensions]
            except Exception as e:
                logger.error(f"Error getting image list: {e}")
                return []

    async def get_image_count(self):
        return len(await self.get_image_list())

    async def delete_image(self, filename):
        async with file_lock:
            try:
                path = os.path.join(self.imgs_folder, filename)
                if os.path.exists(path):
                    await asyncio.to_thread(os.remove, path)
                return True
            except Exception as e:
                logger.error(f"Delete image failed: {e}")
                return False

    async def validate_image(self, path):
        try:
            if not os.path.exists(path):
                return False
            size = os.path.getsize(path)
            if size == 0 or size > self.max_file_size:
                return False
            ext = os.path.splitext(path)[1].lower()
            return ext in self.supported_extensions
        except Exception:
            return False

    async def generate_and_save_image(self, url, filename):
        async with self.download_lock:
            try:
                path = os.path.join(self.imgs_folder, filename)
                if os.path.exists(path):
                    return filename
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=45)
                ) as session:
                    async with session.get(url) as response:
                        if response.status != 200:
                            return None
                        content = await response.read()
                        if not content or len(content) > self.max_file_size:
                            return None
                        async with aiofiles.open(path, "wb") as f:
                            await f.write(content)
                        logger.info(f"Saved image: {filename}")
                        return filename
            except Exception as e:
                logger.error(f"Save image failed: {e}")
                return None

    async def download_one(self):
        """下载一张图入库（缓存池用，无标签随机）。"""
        source = self.config.get("data_source", "lolicon")
        if source == "all":
            return await self._download_one_multi()
        if source == "nyan":
            return await self._download_one_nyan()
        return await self._download_one_lolicon()

    async def _download_one_multi(self):
        """多 provider 降级：按策略试 lolicon 和 nyan。"""
        strategy = self.config.get("provider_strategy", "failover")
        fns = [self._download_one_lolicon, self._download_one_nyan]
        if strategy == "random":
            random.shuffle(fns)
        for fn in fns:
            try:
                r = await fn()
                if r:
                    return r
                logger.warning(f"provider {fn.__name__} returned none, trying next")
            except Exception as e:
                logger.warning(f"provider {fn.__name__} failed: {e}, trying next")
        return None

    async def _download_one_lolicon(self, tags=None, num=1):
        r18 = int(self.config.get("r18", 0))
        if r18 == 2:
            r18 = 1 if random.random() > 0.5 else 0
        results = await fetch_setu_lolicon(
            r18=r18,
            num=num,
            tags=[tags] if tags else [[], []],
            exclude_ai=self.config.get("exclude_ai", True),
            aspect_ratio=self.config.get("aspect_ratio", "gt1") or None,
        )
        if not results:
            return None if not tags else []
        item = results[0]
        filename = f"{item['pid']}_p{item['p']}.{item['ext']}"
        original_url = item["urls"].get("original")
        if not original_url:
            return None if not tags else []
        saved = await self.generate_and_save_image(original_url, filename)
        return saved

    async def download_with_tags(self, num: int, tags: list):
        """带标签实时下载多张（lolicon）。返回文件路径列表。"""
        images = []
        r18 = int(self.config.get("r18", 0))
        if r18 == 2:
            r18 = 1 if random.random() > 0.5 else 0
        results = await fetch_setu_lolicon(
            r18=r18,
            num=max(1, min(20, num)),
            tags=[tags] if tags else [[], []],
            exclude_ai=self.config.get("exclude_ai", True),
            aspect_ratio=self.config.get("aspect_ratio", "gt1") or None,
        )
        if not results:
            return images
        for item in results:
            filename = f"{item['pid']}_p{item['p']}.{item['ext']}"
            original_url = item["urls"].get("original")
            if original_url:
                saved = await self.generate_and_save_image(original_url, filename)
                if saved:
                    images.append(os.path.join(self.imgs_folder, saved))
        return images

    async def _download_one_nyan(self):
        url = "https://sex.nyan.run/api/v2/img"
        params = {"r18": "true" if int(self.config.get("r18", 0)) == 1 else "false"}
        async with self.download_lock:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as session:
                    async with session.get(url, params=params) as response:
                        if response.status != 200:
                            return None
                        content = await response.read()
                        if not content or len(content) < 100 or len(content) > self.max_file_size:
                            return None
                        content_type = response.headers.get('Content-Type', '')
                        ext = self._ext_from_content_type(content_type) or self._ext_from_magic(content)
                        filename = f"{uuid.uuid4().hex[:12]}{ext}"
                        path = os.path.join(self.imgs_folder, filename)
                        async with aiofiles.open(path, 'wb') as f:
                            await f.write(content)
                        if os.path.exists(path) and os.path.getsize(path) > 0:
                            logger.info(f"Saved image (nyan): {filename}")
                            return filename
                        return None
            except Exception as e:
                logger.error(f"nyan download failed: {e}")
                return None

    @staticmethod
    def _ext_from_content_type(ct: str) -> str:
        ct = ct.lower()
        if 'jpeg' in ct or 'jpg' in ct:
            return '.jpg'
        if 'png' in ct:
            return '.png'
        if 'webp' in ct:
            return '.webp'
        if 'gif' in ct:
            return '.gif'
        return ''

    @staticmethod
    def _ext_from_magic(content: bytes) -> str:
        if content.startswith(b'\xff\xd8\xff'):
            return '.jpg'
        if content.startswith(b'\x89PNG\r\n\x1a\n'):
            return '.png'
        if content.startswith(b'GIF87a') or content.startswith(b'GIF89a'):
            return '.gif'
        if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
            return '.webp'
        return '.jpg'

    async def acquire_images(self, num: int, tags: list):
        """获取图片路径：有标签走 lolicon 实时下载，无标签走缓存+补。"""
        if tags:
            return await self.download_with_tags(num, tags)
        images = []
        cached = await self.get_image_list()
        random.shuffle(cached)
        for f in cached[:num]:
            images.append(os.path.join(self.imgs_folder, f))
        need = num - len(images)
        for _ in range(need):
            filename = await self.download_one()
            if filename:
                images.append(os.path.join(self.imgs_folder, filename))
        return images

    async def compress_file(self, path: str, max_bytes: int):
        """读取文件、压缩、写回。"""
        if max_bytes <= 0:
            return
        try:
            async with aiofiles.open(path, 'rb') as f:
                data = await f.read()
            compressed = await compress_image_async(data, max_bytes)
            if compressed != data and len(compressed) < len(data):
                async with aiofiles.open(path, 'wb') as f:
                    await f.write(compressed)
                logger.info(f"Compressed {os.path.basename(path)}: {len(data)} -> {len(compressed)}")
        except Exception as e:
            logger.warning(f"compress failed: {e}")

    async def check_and_refill_cache(self):
        if self._refill_running:
            return
        self._refill_running = True
        try:
            current = await self.get_image_count()
            if current >= self.cache_size:
                return
            need = self.cache_size - current
            logger.info(f"Cache refill: current={current}, need={need}")
            success = 0
            for _ in range(need):
                try:
                    if await self.download_one():
                        success += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Refill item failed: {e}")
            logger.info(f"Cache refill complete: {success}/{need}")
        finally:
            self._refill_running = False

    async def start_background_cache_task(self):
        if self._download_task and not self._download_task.done():
            return
        self._download_task = asyncio.create_task(self._background_cache())

    async def _background_cache(self):
        while True:
            try:
                current = await self.get_image_count()
                if current < self.refill_threshold:
                    await self.check_and_refill_cache()
                await asyncio.sleep(self.refill_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Background cache error: {e}")
                await asyncio.sleep(60)


# ==================== API ====================

async def fetch_setu_lolicon(
    r18: int = 0,
    num: int = 1,
    tags: Optional[List[List[str]]] = None,
    size: List[str] = None,
    uid: List[int] = None,
    keyword: str = None,
    proxy: str = None,
    exclude_ai: bool = None,
    aspect_ratio: str = None
):
    url = "https://api.lolicon.app/setu/v2"
    params = {
        "r18": r18,
        "num": max(1, min(20, num)),
        "excludeAI": exclude_ai,
    }
    if tags:
        params["tag"] = tags
    if size:
        params["size"] = size
    if uid:
        params["uid"] = uid[:20]
    if keyword:
        params["keyword"] = keyword
    if proxy:
        params["proxy"] = proxy
    if aspect_ratio:
        params["aspectRatio"] = aspect_ratio

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.post(url, json=params) as response:
                data = await response.json()
                if data.get("error"):
                    logger.warning(f"API Error: {data['error']}")
                    return None
                return data.get("data", [])
    except Exception as e:
        logger.error(f"fetch_setu error: {e}")
        return None


def match_trigger(text: str, mode: str, words: list) -> bool:
    """根据匹配模式判断是否触发。"""
    text = text.lower()
    if mode == "regex":
        return any(re.search(w, text) for w in words)
    if mode == "exact":
        return any(text.strip() == w.lower() for w in words)
    return any(w.lower() in text for w in words)


@register(
    "astrbot_plugin_lolicon",
    "lolicin",
    "我要涩涩增强版",
    "2.1",
    "https://github.com/lolicin/astrbot_plugin_lolicon"
)
class LoliconPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.image_manager = ImageManager(config)
        asyncio.create_task(self._delayed_start_cache())

    def _msg(self, key: str, **kwargs) -> str:
        """根据 reply_style 返回对应文案，支持占位符。"""
        style = self.config.get("reply_style", "plain")
        playful = {
            "cache_empty": "不准涩涩",
            "sent": "色批给你好了",
            "send_fail": "信号不好没有找到涩涩",
            "error": "处理请求时发生错误，请联系管理员",
            "invalid_count": "看不懂你要几张啦，输入 1 到 {max_count}",
            "max_count_exceeded": "这么多你吃得消吗，最多 {max_count} 张",
            "rate_limited": "急什么，等一下再试",
            "fetch_timeout": "信号太差没找到涩涩",
            "tag_ignored": "太多了顾不上标签啦",
            "tag_not_found": "没有找到这些标签的图，随机给你来点涩图吧",
        }
        plain = {
            "cache_empty": "库存为空，正在补货，请稍后再试",
            "sent": "给你涩图~",
            "send_fail": "图片发送失败",
            "error": "处理请求时发生错误",
            "invalid_count": "数量无效，请输入 1 到 {max_count} 之间的数字",
            "max_count_exceeded": "一次最多 {max_count} 张哦",
            "rate_limited": "你太快啦，等一下再试",
            "fetch_timeout": "获取超时，请稍后再试",
            "tag_ignored": "多图已忽略标签，走本地缓存（更快）",
            "tag_not_found": "没有找到与标签 {tags} 匹配的图片，已为你随机取图",
        }
        table = playful if style == "playful" else plain
        text = table.get(key, plain.get(key, ""))
        if kwargs:
            try:
                return text.format(**kwargs)
            except Exception:
                return text
        return text

    def _with_pids(self, text: str, filenames: list) -> str:
        """按配置在回复文本后附加 pid（多图逗号分隔）。"""
        if not bool(self.config.get("show_pid", False)):
            return text
        pids = collect_pids(filenames)
        if not pids:
            return text
        label = self.config.get("pid_label", "PID: ")
        joined = ",".join(pids)
        sep = "\n" if bool(self.config.get("pid_newline", True)) else " "
        return f"{text}{sep}{label}{joined}"

    async def _delayed_start_cache(self):
        await asyncio.sleep(5)
        await self.image_manager.check_and_refill_cache()
        await self.image_manager.start_background_cache_task()

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            text = event.message_str
            mode = self.config.get("match_mode", "contains")
            words = self.config.get("trigger_words", ["色图", "涩图", "瑟图"])
            if match_trigger(text, mode, words):
                count_str, tags = extract_count_and_tags(text, words)
                return await self.handle_image_request(event, count_str, tags)
        except Exception as e:
            logger.error(f"Message handler error: {e}")
            return event.plain_result(f"插件异常: {e}")

    async def handle_image_request(self, event, count_str: str, tags: list):
        user_id = "default"
        try:
            user_id = event.get_sender_id()
        except Exception:
            pass

        if not await _rate_limiter.acquire(user_id):
            return event.plain_result(self._msg("rate_limited"))

        sent_basenames = []
        result = None
        try:
            max_count = int(self.config.get("max_count", 5))
            num = parse_count(count_str)
            if num == -1:
                num = 1
                if count_str:
                    tags = tags + [count_str]
            if num < 1:
                return event.plain_result(self._msg("invalid_count", max_count=max_count))
            if num > max_count:
                return event.plain_result(self._msg("max_count_exceeded", max_count=max_count))

            alias_map = parse_alias_map(self.config.get("tag_alias", ""))
            resolved_tags = resolve_tags(tags, alias_map)

            # 多图带标签时的处理方式（配置项 multi_tag_mode）
            tag_ignored = False
            if resolved_tags and num > 1:
                mt_mode = self.config.get("multi_tag_mode", "ignore_tag")
                if mt_mode != "fetch_by_tag":
                    resolved_tags = []
                    tag_ignored = True

            images = await self.image_manager.acquire_images(num, resolved_tags)

            # 标签搜不到图时回退随机图（走缓存池），并附加提示
            tag_fallback = False
            if not images and resolved_tags:
                logger.info(f"tag search returned nothing for {resolved_tags}, falling back to random")
                tag_fallback = True
                images = await self.image_manager.acquire_images(num, [])

            if not images:
                asyncio.create_task(self.image_manager.check_and_refill_cache())
                return event.plain_result(self._msg("cache_empty"))

            max_bytes = int(self.config.get("max_image_bytes", 0))
            sent_count = 0
            for img_path in images:
                try:
                    if max_bytes > 0:
                        await self.image_manager.compress_file(img_path, max_bytes)
                    await event.send(event.make_result().file_image(img_path))
                    sent_basenames.append(os.path.basename(img_path))
                    sent_count += 1
                except asyncio.TimeoutError:
                    logger.warning("send timeout")
                    result = event.plain_result(self._msg("fetch_timeout"))
                    break
                except Exception as e:
                    logger.error(f"send image error: {e}")

            if sent_count > 0:
                asyncio.create_task(self.image_manager.check_and_refill_cache())
                if tag_fallback:
                    text = f"{self._msg('tag_not_found', tags=', '.join(tags))} x{sent_count}"
                elif tag_ignored:
                    text = self._msg("tag_ignored")
                else:
                    text = f"{self._msg('sent')} x{sent_count}"
                result = event.plain_result(self._with_pids(text, sent_basenames))

        except asyncio.TimeoutError:
            logger.warning("handle_image_request timeout")
            result = event.plain_result(self._msg("fetch_timeout"))
        except Exception as e:
            logger.error(f"handle_image_request failed: {e}")
            result = event.plain_result(self._msg("error"))
        finally:
            await _rate_limiter.release(user_id)
            for bn in sent_basenames:
                await self.image_manager.delete_image(bn)

        if result:
            return result

    async def terminate(self):
        try:
            if self.image_manager._download_task:
                self.image_manager._download_task.cancel()
            files = await self.image_manager.get_image_list()
            if files:
                await asyncio.gather(
                    *(self.image_manager.delete_image(f) for f in files)
                )
            logger.info("Plugin terminated, cleaned up %d images", len(files))
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
