import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, sync_playwright
from tqdm import tqdm

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


@dataclass
class ChapterInfo:
    author: str
    title: str
    chapter: str


def _cap_words(slug: str) -> str:
    # 每个单词首字母大写，其余保持原样；不用 str.title()，避免 "daval3dc" -> "Daval3Dc"
    return " ".join(w[:1].upper() + w[1:] for w in slug.split("-") if w)


def _pad_chapter(chapter: str) -> str:
    # 整数部分至少两位：1 -> 01，10 -> 10，1.5 -> 01.5，100 -> 100
    head, sep, tail = chapter.partition(".")
    return head.zfill(2) + sep + tail


def parse_chapter_url(url: str) -> ChapterInfo:
    parts = [p for p in urlparse(url).path.split("/") if p]
    # 期望格式: /porncomic/<title>-<author>/<chapter>-<title>/
    if len(parts) < 3 or parts[0] != "porncomic":
        raise ValueError(f"无法识别的章节 URL: {url}")
    comic_slug, chapter_slug = parts[1], parts[2]

    m = re.match(r"^(\d+(?:\.\d+)?)-?(.*)$", chapter_slug)
    if not m:
        raise ValueError(f"章节 slug 中没有章节号: {chapter_slug}")
    chapter, title_slug = m.group(1), m.group(2)

    if title_slug and comic_slug.startswith(title_slug + "-"):
        author_slug = comic_slug[len(title_slug) + 1 :]
    else:
        # 兜底：章节 slug 不含标题时，假设作者是最后一段
        title_slug, _, author_slug = comic_slug.rpartition("-")

    return ChapterInfo(
        author=_cap_words(author_slug),
        title=_cap_words(title_slug),
        chapter=_pad_chapter(chapter),
    )


def build_folder(info: ChapterInfo, root: Path | None = None) -> Path:
    root = root or Path.home()
    return root / info.author / info.title / info.chapter


IMG_ID_RE = re.compile(r"^image-(\d+)$")


def _pick_http_url(img, base_url: str) -> str | None:
    """依次检查 data-src、src，返回第一个 http(s) 地址；data: 占位图会被跳过。"""
    for attr in ("data-src", "src"):
        raw = img.get(attr)
        if not isinstance(raw, str):
            continue
        raw = raw.strip()  # data-src 常带换行/制表符
        if not raw or raw.startswith("data:"):
            continue
        full = urljoin(base_url, raw)
        if urlparse(full).scheme in ("http", "https"):
            return full
    return None


def get_image_urls(context: BrowserContext, page_url: str) -> tuple[list[str], str]:
    page = context.new_page()
    try:
        page.goto(page_url, wait_until="domcontentloaded")
        page.wait_for_selector("img[id^='image-']", state="attached", timeout=10000)
        html = page.content()
        base_url = page.url  # 重定向后的最终地址
    finally:
        page.close()

    soup = BeautifulSoup(html, "html.parser")

    # 收集 id 形如 image-0, image-1 ... 的 img，并按数字排序
    indexed: list[tuple[int, object]] = []
    for img in soup.find_all("img", id=IMG_ID_RE):
        if img.find_parent("noscript"):  # 跳过 <noscript> 里的重复 img
            continue
        m = IMG_ID_RE.match(img["id"])  # type: ignore
        if m:
            indexed.append((int(m.group(1)), img))
    indexed.sort(key=lambda t: t[0])

    urls: list[str] = []
    seen: set[str] = set()
    for idx, img in indexed:
        url = _pick_http_url(img, base_url)
        if not url:
            print(f"image-{idx} 没有有效的 http(s) 地址，已跳过")
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)

    return urls, base_url


def download(
    context: BrowserContext,
    chapter_url: str,
    image_urls: list[str],
    folder: Path,
    delay_ms: int = 1500,
    retries: int = 3,
) -> list[int]:
    folder.mkdir(parents=True, exist_ok=True)

    # 先访问章节页，让会话/cookie 和正常浏览一致
    page = context.new_page()
    page.goto(chapter_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    failed: list[int] = []
    consecutive_fail = 0
    done = skipped = 0

    # 进度条运行期间用 tqdm.write 输出日志，避免把进度条打断成多行
    bar = tqdm(total=len(image_urls), desc="下载", unit="张", dynamic_ncols=True)
    try:
        for i, u in enumerate(image_urls, start=1):
            ext = Path(urlparse(u).path).suffix or ".webp"
            target = folder / f"{i:03d}{ext}"
            if target.exists() and target.stat().st_size > 0:
                skipped += 1
                bar.set_postfix({"成功": done, "跳过": skipped, "失败": len(failed)})
                bar.update(1)
                continue  # 已存在的不需要等待 delay

            ok = False
            for attempt in range(1, retries + 1):
                try:
                    resp = page.goto(u, referer=chapter_url, wait_until="load")
                    if resp and resp.ok:
                        target.write_bytes(resp.body())
                        ok = True
                        consecutive_fail = 0
                        break
                    status = resp.status if resp else None
                    tqdm.write(f"[{i}] HTTP {status}，第 {attempt} 次")
                    if status == 403:
                        page.wait_for_timeout(3000 * attempt)  # 403 时退避更久
                except Exception as e:
                    tqdm.write(f"[{i}] 异常：{e}，第 {attempt} 次")
                    page.wait_for_timeout(2000)

            if ok:
                done += 1
            else:
                failed.append(i)
                consecutive_fail += 1

            bar.set_postfix({"成功": done, "跳过": skipped, "失败": len(failed)})
            bar.update(1)

            if consecutive_fail >= 5:
                tqdm.write("连续 5 张失败，疑似被限速/拦截，先停止。已下载的下次会自动跳过。")
                failed.extend(range(i + 1, len(image_urls) + 1))
                break

            page.wait_for_timeout(delay_ms)
    finally:
        bar.close()
        page.close()

    return failed


def main() -> None:
    ap = argparse.ArgumentParser(description="按章节 URL 下载漫画")
    ap.add_argument("url", help="章节页 URL")
    ap.add_argument("--root", type=Path, default=Path(Path.home() / "Comic"), help="保存根目录，默认用户主目录")
    args = ap.parse_args()

    info = parse_chapter_url(args.url)
    folder = build_folder(info, args.root)
    print(f"{info.author} / {info.title} / 第 {info.chapter} 章 -> {folder}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chromium",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
        )
        try:
            image_urls, referer = get_image_urls(context, args.url)
            print(f"共找到 {len(image_urls)} 张图片")
            failed = download(context, args.url, image_urls, folder)
            if failed:
                print("下载失败的页码：", failed)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
