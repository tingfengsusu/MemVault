"""
B站视频下载器 — 直接调用 B站 API，无需 yt-dlp / ffmpeg。

技术流程（参考 DownKyi 实现思路）：
  1. 从 URL 提取 BV 号
  2. 调用 pagelist API → 获取 cid
  3. 调用 playurl API → 获取 durl (预合并分段) 或 dash (分离音视频)
  4. 分段下载 + 断点续传
  5. durl 模式直接二进制拼接; dash 模式只下载视频流（无需音频，抽帧不需要）

使用示例:
    from bili_downloader import BiliDownloader

    dl = BiliDownloader(cookies_path="cookies.txt")
    video_path = dl.download("https://www.bilibili.com/video/BV1xxx")
    print(f"下载完成: {video_path}")
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from memvault.utils import get_ffmpeg_path

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

BILI_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

# 画质映射: qn → 分辨率描述
QUALITY_MAP = {
    127: "8K",
    120: "4K",
    116: "1080P60",
    112: "1080P+",
    80: "1080P",
    74: "720P60",
    64: "720P",
    32: "480P",
    16: "360P",
    6: "240P",
}

# 只请求 durl (预合并流)，fnval=1 避免触发 DASH 合并
FNFAL_DURL_ONLY = 1
# 同时请求 durl + dash，用于 durl 不可用时的降级
FNFAL_BOTH = 80

# 下载分片大小 (5MB)
CHUNK_SIZE = 5 * 1024 * 1024


# ── Cookie 解析 ──────────────────────────────────────────────────────────────

class BiliCookie:
    """解析 Netscape 格式的 cookies.txt，提取 B站 鉴权字段。"""

    @staticmethod
    def parse_file(cookies_path: str) -> Dict[str, str]:
        """读取 cookies.txt，返回 {name: value} 字典。"""
        cookies = {}
        path = Path(cookies_path)
        if not path.exists():
            logger.warning(f"cookies 文件不存在: {cookies_path}")
            return cookies

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # 跳过注释和空行
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain, _, _, _, _, name, value = parts[:7]
                    # 只保留 bilibili 域下的 cookie
                    if "bilibili" in domain:
                        cookies[name] = value

        logger.info(
            f"加载 {len(cookies)} 个 B站 cookies"
            + (" [含 SESSDATA ✓]" if "SESSDATA" in cookies else " [缺 SESSDATA ⚠]")
        )
        return cookies

    @staticmethod
    def to_header(cookies_path: str) -> str:
        """将 cookies.txt 转为 HTTP Cookie 头的值。"""
        cookies = BiliCookie.parse_file(cookies_path)
        return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ── B站 API 客户端 ──────────────────────────────────────────────────────────

class BiliAPI:
    """封装 B站 后端 API 调用。"""

    def __init__(self, cookies_path: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update(BILI_API_HEADERS)
        if cookies_path:
            self.session.headers["Cookie"] = BiliCookie.to_header(cookies_path)

    def _get(self, url: str, params: dict = None, retries: int = 3) -> dict:
        """带重试的 GET 请求。"""
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(
                        f"B站 API 返回错误 (code={data.get('code')}): {data.get('message', 'unknown')}"
                    )
                return data
            except requests.exceptions.RequestException as e:
                if attempt < retries - 1:
                    wait = (attempt + 1) * 2
                    logger.warning(f"API 请求失败，{wait}s 后重试: {e}")
                    time.sleep(wait)
                else:
                    raise

    def get_page_list(self, bvid: str) -> List[Dict]:
        """获取视频分P列表，返回 [{cid, page, part}, ...]。
        API: https://api.bilibili.com/x/player/pagelist?bvid=BVxxx
        """
        data = self._get(
            "https://api.bilibili.com/x/player/pagelist",
            params={"bvid": bvid},
        )
        return data.get("data", [])

    def get_video_info(self, bvid: str) -> Dict:
        """获取视频基本信息（标题、封面等）。
        API: https://api.bilibili.com/x/web-interface/view?bvid=BVxxx
        """
        data = self._get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
        )
        return data.get("data", {})

    def get_play_url(
        self, bvid: str, cid: int, qn: int = 64, fnval: int = FNFAL_BOTH
    ) -> Dict:
        """获取视频播放地址，返回 durl 和/或 dash 数据。
        API: https://api.bilibili.com/x/player/playurl

        参数:
            bvid: BV 号
            cid: 分P的 cid
            qn: 画质编码 (默认 64 = 720P)
            fnval: 流格式 (1=仅durl, 80=durl+dash)

        返回: {"durl": [...], "dash": {...}, "quality": ..., "accept_quality": [...]}
        """
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": qn,
            "fnval": fnval,
            "fnver": 0,
            "fourk": 1,
        }
        data = self._get(
            "https://api.bilibili.com/x/player/playurl",
            params=params,
        )
        return data.get("data", {})


# ── 分段下载器（含断点续传）─────────────────────────────────────────────────

class SegmentDownloader:
    """分段下载 + 断点续传。

    工作目录结构:
        .checkpoints/{video_hash}/
        ├── manifest.json          # 下载清单（URL→文件名映射 + 状态）
        ├── seg_0001.flv           # 已完成的段
        └── seg_0001.flv.part      # 下载中的段
    """

    def __init__(self, checkpoint_dir: Path, task_id: str):
        self.task_dir = checkpoint_dir / task_id
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.task_dir / "manifest.json"
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict:
        if self.manifest_path.exists():
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"files": {}, "completed": []}

    def _save_manifest(self):
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)

    def download_segments(
        self, urls: List[str], ext: str = ".mp4", session: requests.Session = None
    ) -> List[Path]:
        """下载多个分段 URL，支持断点续传。

        返回: 本地文件路径列表（按 urls 顺序）
        """
        if session is None:
            session = requests.Session()

        local_paths = []
        total = len(urls)

        for i, url in enumerate(urls):
            # 用 URL hash 做稳定文件名
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            filename = f"seg_{i:04d}_{url_hash}{ext}"
            local_path = self.task_dir / filename

            # 已经在 manifest 中标记完成 → 跳过
            if url in self.manifest["completed"] and local_path.exists():
                logger.info(f"  [{i+1}/{total}] 跳过(已完成): {filename}")
                local_paths.append(local_path)
                continue

            # 下载
            part_path = Path(str(local_path) + ".part")
            self._download_single(url, part_path, session, i, total)

            # 完成后重命名
            part_path.rename(local_path)
            self.manifest["files"][url] = filename
            self.manifest["completed"].append(url)
            self._save_manifest()

            local_paths.append(local_path)

        return local_paths

    def _download_single(
        self,
        url: str,
        dest_path: Path,
        session: requests.Session,
        index: int,
        total: int,
    ):
        """下载单个文件，支持 Range 续传。"""
        headers = {"Referer": "https://www.bilibili.com/"}

        # 检测已有部分下载
        resume_pos = 0
        if dest_path.exists():
            resume_pos = dest_path.stat().st_size
            if resume_pos > 0:
                headers["Range"] = f"bytes={resume_pos}-"

        for attempt in range(3):
            try:
                resp = session.get(url, headers=headers, stream=True, timeout=60)
                if resp.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {resp.status_code}")

                total_size = resume_pos + int(
                    resp.headers.get("Content-Length", 0)
                )
                mode = "ab" if resume_pos > 0 else "wb"

                with open(dest_path, mode) as f:
                    downloaded = resume_pos
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                pct = downloaded / total_size * 100
                                print(
                                    f"\r  [{index+1}/{total}] {dest_path.name}  "
                                    f"{self._fmt_size(downloaded)}/{self._fmt_size(total_size)} "
                                    f"({pct:.0f}%)",
                                    end="",
                                )
                print()  # 换行
                return  # 成功

            except Exception as e:
                if attempt < 2:
                    logger.warning(f"下载失败，重试 ({attempt+2}/3): {e}")
                    time.sleep(3)
                    resume_pos = dest_path.stat().st_size if dest_path.exists() else 0
                    if resume_pos > 0:
                        headers["Range"] = f"bytes={resume_pos}-"
                else:
                    raise

    def download_single(
        self,
        url: str,
        filename: str,
        session: requests.Session = None,
    ) -> Path:
        """下载单个大文件（DASH 视频流/音频流），带断点续传。"""
        if session is None:
            session = requests.Session()

        output_path = self.task_dir / filename
        part_path = Path(str(output_path) + ".part")

        if output_path.exists():
            logger.info(f"  跳过(已完成): {filename}")
            return output_path

        self._download_single(url, part_path, session, 0, 1)
        part_path.rename(output_path)
        return output_path

    def cleanup(self):
        """删除检查点数据。"""
        if self.task_dir.exists():
            shutil.rmtree(self.task_dir, ignore_errors=True)

    @staticmethod
    def _fmt_size(size: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"


# ── 主下载器 ─────────────────────────────────────────────────────────────────

class BiliDownloader:
    """B站视频直接下载器。

    使用示例:
        # 不需要 cookie（公开视频）
        dl = BiliDownloader()
        path = dl.download("https://www.bilibili.com/video/BV1xxx")

        # 带 cookie（高清/会员视频）
        dl = BiliDownloader(cookies_path="cookies.txt")
        path = dl.download("BV1xxx", quality=80)
    """

    def __init__(
        self,
        cookies_path: Optional[str] = None,
        output_dir: str = "./temp/videos",
        prefer_durl: bool = True,
    ):
        self.api = BiliAPI(cookies_path=cookies_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.prefer_durl = prefer_durl
        self.checkpoint_dir = Path("./temp/checkpoints")
        # 最近一次 download() 拿到的视频元信息(标题/UP主/BV 号),供管线上层使用
        self.last_meta: dict = {}

    # ── 公开 API ─────────────────────────────────────────────────────────

    def download(
        self,
        url: str,
        quality: int = 64,
        output_name: Optional[str] = None,
    ) -> Path:
        """下载 B站 视频到本地。

        参数:
            url: B站视频链接（完整 URL 或 BV 号）
            quality: 画质代号 (64=720P, 80=1080P 等，见 QUALITY_MAP)
            output_name: 输出文件名（不含扩展名），默认用视频标题

        返回: 本地视频文件路径 (.mp4 或 .m4s)
        """
        bvid = self._parse_bvid(url)
        logger.info(f"BV 号: {bvid}")

        # 1. 获取视频信息
        info = self.api.get_video_info(bvid)
        title = info.get("title", bvid)
        owner = info.get("owner") or {}
        self.last_meta = {"bvid": bvid, "title": title,
                          "up": owner.get("name"), "up_mid": owner.get("mid")}
        # 清理文件名中的非法字符
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
        logger.info(f"视频标题: {safe_title}")

        # 2. 获取 cid
        page_list = self.api.get_page_list(bvid)
        if page_list:
            self.last_meta["cid"] = page_list[0].get("cid")
        if not page_list:
            raise RuntimeError(f"无法获取分P信息，请检查 BV 号: {bvid}")
        cid = page_list[0]["cid"]
        logger.info(f"cid: {cid} (共 {len(page_list)} 个分P)")

        # 3. 获取播放地址
        fnval = FNFAL_DURL_ONLY if self.prefer_durl else FNFAL_BOTH
        play_data = self.api.get_play_url(bvid, cid, qn=quality, fnval=FNFAL_BOTH)
        # 先尝试获取完整信息

        output_filename = output_name or safe_title
        output_path = self.output_dir / f"{output_filename}.mp4"

        # 4. 优先 durl（预合并，无需 ffmpeg），不可用时降级到 dash
        durl_list = play_data.get("durl", [])
        dash_data = play_data.get("dash")

        if durl_list:
            logger.info(
                f"使用 durl 模式（预合并流，{len(durl_list)} 个分段），无需 ffmpeg"
            )
            return self._download_durl(durl_list, output_path, title)

        elif dash_data:
            logger.info("durl 不可用，降级到 DASH 模式（仅视频流，无需音频）")
            return self._download_dash(
                dash_data, output_path, title, quality
            )

        else:
            # 尝试降低画质重新请求
            logger.warning("当前画质无可用流，尝试降级...")
            accept_qualities = play_data.get("accept_quality", [quality])
            for qn in accept_qualities[1:]:  # 跳过已尝试的第一个
                logger.info(f"尝试画质: {QUALITY_MAP.get(qn, qn)}")
                play_data = self.api.get_play_url(bvid, cid, qn=qn, fnval=FNFAL_BOTH)
                durl_list = play_data.get("durl", [])
                if durl_list:
                    desc = QUALITY_MAP.get(qn, str(qn))
                    logger.info(f"使用 durl ({desc}，{len(durl_list)} 个分段)")
                    return self._download_durl(durl_list, output_path, title)
                dash_data = play_data.get("dash")
                if dash_data:
                    logger.info(f"使用 DASH ({QUALITY_MAP.get(qn, qn)})")
                    return self._download_dash(
                        dash_data, output_path, title, qn
                    )

            raise RuntimeError("所有画质均无可用的播放流，请尝试提供 cookies 或更换视频。")

    # ── 内部实现 ─────────────────────────────────────────────────────────

    def _parse_bvid(self, url: str) -> str:
        """从各种 B站 URL 格式提取 BV 号。"""
        url = url.strip()

        # 纯 BV 号
        if re.match(r"^BV[a-zA-Z0-9]+$", url):
            return url

        # 完整 URL
        patterns = [
            r"bilibili\.com/video/(BV[a-zA-Z0-9]+)",
            r"bilibili\.com/bangumi/play/ss\d+\?.*",
            r"b23\.tv/([a-zA-Z0-9]+)",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                bvid = m.group(1)
                # b23.tv 短链接需跟随重定向
                if "b23.tv" in url:
                    resp = requests.head(url, allow_redirects=True, timeout=10)
                    return self._parse_bvid(resp.url)
                return bvid

        raise ValueError(f"无法从 URL 中提取 BV 号: {url}")

    def _download_durl(
        self,
        durl_list: List[Dict],
        output_path: Path,
        title: str,
    ) -> Path:
        """下载 durl 分段（预合并的 FLV/MP4），二进制拼接。"""
        urls = [seg["url"] for seg in durl_list]
        # 检测扩展名
        sample_url = urls[0]
        ext = ".flv" if ".flv" in sample_url else ".mp4"

        task_id = hashlib.md5((title + "_durl").encode()).hexdigest()[:12]
        downloader = SegmentDownloader(self.checkpoint_dir, task_id)

        logger.info(f"下载 {len(urls)} 个 durl 分段 (格式: {ext})")
        segments = downloader.download_segments(urls, ext=ext, session=self.api.session)

        # 二进制拼接所有分段
        logger.info(f"拼接分段 → {output_path.name}")
        with open(output_path, "wb") as out:
            for seg in segments:
                with open(seg, "rb") as f:
                    shutil.copyfileobj(f, out)

        # 验证
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(f"durl 下载完成: {output_path} ({size_mb:.1f} MB)")

        # 清理检查点
        if not self.checkpoint_dir.exists():
            pass  # checkpoint_dir 可能已被清理
        elif any(self.checkpoint_dir.iterdir()):
            pass
        downloader.cleanup()

        return output_path

    def _download_dash(
        self,
        dash_data: Dict,
        output_path: Path,
        title: str,
        quality: int,
    ) -> Path:
        """下载 DASH 音视频流，用 ffmpeg 合并为 MP4。"""
        videos = dash_data.get("video", [])
        audios = dash_data.get("audio", [])

        if not videos:
            raise RuntimeError("DASH 数据中没有视频流")

        video_info = videos[0]
        video_url = video_info.get("baseUrl") or video_info.get("base_url")
        if not video_url:
            raise RuntimeError("DASH 视频流 URL 缺失")

        audio_url = None
        if audios:
            audio_info = audios[0]
            audio_url = audio_info.get("baseUrl") or audio_info.get("base_url")

        task_id = hashlib.md5((title + "_dash").encode()).hexdigest()[:12]
        downloader = SegmentDownloader(self.checkpoint_dir, task_id)

        desc = QUALITY_MAP.get(quality, str(quality))
        logger.info(f"DASH 下载 ({desc}): 视频 {video_info.get('codecs', '?')}")

        video_file = downloader.download_single(
            video_url, "video.m4s", session=self.api.session
        )

        if audio_url:
            logger.info(f"DASH 下载: 音频 {audio_info.get('codecs', '?')}")
            audio_file = downloader.download_single(
                audio_url, "audio.m4s", session=self.api.session
            )
            logger.info(f"ffmpeg 合并音视频 → {output_path.name}")
            ffmpeg_path = get_ffmpeg_path()
            subprocess.run(
                [
                    ffmpeg_path, "-y",
                    "-i", str(video_file),
                    "-i", str(audio_file),
                    "-c", "copy",
                    str(output_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            downloader.cleanup()
            size_mb = output_path.stat().st_size / (1024 * 1024)
            logger.info(f"DASH 下载完成: {output_path.name} ({size_mb:.1f} MB)")
            return output_path
        else:
            size_mb = video_file.stat().st_size / (1024 * 1024)
            logger.info(f"DASH 下载完成 (无音频流): {video_file.name} ({size_mb:.1f} MB)")
            downloader.cleanup()
            return video_file


# ── 便捷函数 ─────────────────────────────────────────────────────────────────

def download_bili_video(
    url: str,
    output_dir: str = "./temp/videos",
    cookies_path: Optional[str] = None,
    quality: int = 64,
    prefer_durl: bool = True,
) -> Path:
    """一行下载 B站 视频。

    参数:
        url: B站视频链接
        output_dir: 输出目录
        cookies_path: cookies.txt 路径（可选，用于高清/会员视频）
        quality: 画质 (64=720P, 80=1080P)
        prefer_durl: 优先使用免 ffmpeg 的 durl 模式

    返回: 本地视频文件路径
    """
    dl = BiliDownloader(
        cookies_path=cookies_path,
        output_dir=output_dir,
        prefer_durl=prefer_durl,
    )
    return dl.download(url, quality=quality)


# ── 自测入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("用法: python bili_downloader.py <B站视频URL或BV号> [cookies.txt]")
        print("示例: python bili_downloader.py BV1ML4y1W7Bd cookies.txt")
        sys.exit(1)

    test_url = sys.argv[1]
    cookies = sys.argv[2] if len(sys.argv) > 2 else None

    result = download_bili_video(
        url=test_url,
        output_dir="./temp/test_download",
        cookies_path=cookies,
        quality=64,  # 720P
    )
    print(f"\n✅ 下载成功: {result}")
