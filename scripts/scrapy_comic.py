from pathlib import Path
from time import sleep
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import BrowserContext, sync_playwright

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"


def get_image_urls(context: BrowserContext, page_url: str) -> tuple[list[str], str]:
    page = context.new_page()
    try:
        page.goto(page_url, wait_until="domcontentloaded")
        page.wait_for_selector(
            "div.page-break img.wp-manga-chapter-img",
            state="attached",
            timeout=10000,
        )
        html = page.content()
        base_url = page.url  # 重定向后的最终地址
    finally:
        page.close()

    soup = BeautifulSoup(html, "html.parser")

    urls: list[str] = []
    seen: set[str] = set()
    for img in soup.select("div.page-break img.wp-manga-chapter-img"):
        if img.find_parent("noscript"):
            continue
        raw = img.get("data-src") or img.get("src")
        if not isinstance(raw, str):
            continue
        raw = raw.strip()  # Madara 主题的 data-src 常带换行/制表符
        if not raw or raw.startswith("data:"):
            continue
        full = urljoin(base_url, raw)
        if full not in seen:
            seen.add(full)
            urls.append(full)

    return urls, base_url


def download(
    context: BrowserContext,
    chapter_url: str,
    image_urls: list[str],
    folder_name: str = "Secret Desires",
    delay_ms: int = 1500,
    retries: int = 3,
) -> list[int]:
    folder = Path(folder_name)
    folder.mkdir(parents=True, exist_ok=True)

    # 先访问章节页，让会话/cookie 和正常浏览一致
    page = context.new_page()
    page.goto(chapter_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    failed: list[int] = []
    consecutive_403 = 0

    for i, u in enumerate(image_urls, start=1):
        ext = Path(urlparse(u).path).suffix or ".webp"
        target = folder / f"{i:03d}{ext}"
        if target.exists() and target.stat().st_size > 0:
            continue

        ok = False
        for attempt in range(1, retries + 1):
            try:
                resp = page.goto(u, referer=chapter_url, wait_until="load")
                if resp and resp.ok:
                    target.write_bytes(resp.body())
                    ok = True
                    consecutive_403 = 0
                    break
                status = resp.status if resp else None
                print(f"[{i}] HTTP {status}，第 {attempt} 次")
                if status == 403:
                    page.wait_for_timeout(3000 * attempt)  # 403 时退避更久
            except Exception as e:
                print(f"[{i}] 异常：{e}，第 {attempt} 次")
                page.wait_for_timeout(2000)

        if not ok:
            failed.append(i)
            consecutive_403 += 1
            if consecutive_403 >= 5:
                print("连续 5 张失败，疑似被限速/拦截，先停止。已下载的下次会自动跳过。")
                failed.extend(range(i + 1, len(image_urls) + 1))
                break

        page.wait_for_timeout(delay_ms)

    page.close()
    return failed


if __name__ == "__main__":
    chapter_url = "https://novelcrow.com/comic/secret-desires-nandof/1-secret-desires-chapter-1-nandof/"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chromium",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
        )
        try:
            image_urls, referer = get_image_urls(context, chapter_url)
            print(f"共找到 {len(image_urls)} 张图片")
            failed = download(context, chapter_url, image_urls)
            if failed:
                print("下载失败的页码：", failed)
        finally:
            context.close()
            browser.close()
