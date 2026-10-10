import argparse
import logging
import mimetypes
import random
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import psycopg
import uuid_utils as uuid
import yaml
from bs4 import BeautifulSoup
from minio import Minio
from playwright.sync_api import sync_playwright

logger = logging.getLogger("skill_import")

FRONTMATTER_RE = re.compile(r"\A\ufeff?---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.DOTALL)
WORK_DIR = Path("/Users/kakarot/Skills")
BASE_PATH = WORK_DIR / ".claude" / "skills"
INSTALL_TIMEOUT = 120

DB_DSN = "postgresql://postgres:112233@localhost:5432/marus"
S3_ENDPOINT = "localhost:9000"
S3_ACCESS_KEY = "access_key"
S3_SECRET_KEY = "secret_key"
S3_SECURE = False
S3_BUCKET = "skill-files"

EXTRA_TYPES = {
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".toml": "text/plain",
}


class Args(argparse.Namespace):
    url: str
    display_name: str
    example_prompts: list[str]
    tags: list[str]
    verbose: bool
    author: str


@dataclass
class Skill:
    id: str
    display_name: str
    name: str
    description: str
    skill_md_text: str
    install_count: int
    example_prompts: list[str]
    tags: list[str]
    root: Path
    files: list[Path]
    author: str

    @property
    def cover_key(self) -> str:
        return f"skills/{self.id}/cover.webp"

    def storage_key(self, file: Path) -> str:
        return f"skills/{self.id}/files/{self.relative(file)}"

    def relative(self, file: Path) -> str:
        return file.relative_to(self.root).as_posix()


# ---------- CLI / logging ----------


def parse_args() -> Args:
    parser = argparse.ArgumentParser(description="把 Agent Skills 保存到数据库, 并把文件上传到对象存储")
    parser.add_argument("--url", type=str, required=True, help="Skill 的详情地址")
    parser.add_argument("--display-name", type=str, required=True, help="Skill 显示名")
    parser.add_argument("--tags", type=str, nargs="+", required=True, help="Skill 分类")
    parser.add_argument("--example-prompts", nargs="+", default=[], help="示例提示词, 每条用引号包起来")
    parser.add_argument("--author", type=str, default="Manu", help="示例提示词, 每条用引号包起来")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 级别日志")
    return parser.parse_args(namespace=Args())


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(funcName)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


# ---------- 抓取页面 ----------


def fetch_html(url: str) -> str:
    logger.info("启动浏览器抓取页面: %s", url)
    start = time.perf_counter()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("code:has-text('npx skills add')")
            html = page.content()
            logger.info("页面抓取完成: %d 字符, 耗时 %.2fs", len(html), time.perf_counter() - start)
            return html
        finally:
            browser.close()


def get_cmd(soup: BeautifulSoup) -> str:
    for code in soup.find_all("code"):
        if "npx skills add" in code.get_text():
            cmd = "".join(code.find_all(string=True, recursive=False)).strip()
            logger.info("解析到安装指令: %s", cmd)
            return cmd
    raise RuntimeError("未找到安装指令")


def parse_count(s: str) -> int:
    m = re.fullmatch(r"([\d.,]+)\s*([KMB]?)", s.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"无法解析: {s!r}")

    num = Decimal(m.group(1).replace(",", ""))
    suffix = m.group(2).upper()
    unit = {"": 1, "K": 10**3, "M": 10**6, "B": 10**9}[suffix]
    base = int(num * unit)

    if not suffix:  # 没有单位,说明是精确值,不随机
        return base

    decimals = max(0, -num.as_tuple().exponent)  # type: ignore
    step = unit // 10**decimals
    return base + random.randrange(step)


def get_installs(soup: BeautifulSoup) -> int:
    for label in soup.find_all("div", class_="uppercase"):
        if label.get_text(strip=True).lower() == "installs":
            value = label.find_next_sibling("div")
            if value:
                raw = value.get_text(strip=True)
                count = parse_count(raw)
                logger.info("安装量: 原始值=%r, 解析后=%d", raw, count)
                return count
    logger.warning("页面中未找到 installs 字段, 安装量按 0 处理")
    return 0


def scrape_skill_page(url: str) -> tuple[str, int]:
    """返回 (安装指令, 安装量)。"""
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    return get_cmd(soup), get_installs(soup)


# ---------- 安装 ----------


def parse_skill_name(cmd: str) -> str:
    tokens = shlex.split(cmd)
    if tokens[:3] != ["npx", "skills", "add"]:
        raise ValueError(f"不是预期的命令: {cmd}")

    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("source")
    parser.add_argument("-s", "--skill", action="append", default=[])
    ns, _ = parser.parse_known_args(tokens[3:])
    if not ns.skill:
        raise ValueError(f"安装指令缺少 --skill: {cmd}")
    logger.info("解析安装指令: source=%s, skill=%s", ns.source, ns.skill[0])
    return ns.skill[0]


def install_skill(cmd: str, skill_name: str) -> Path:
    """执行安装命令, 返回 skill 目录; SKILL.md 缺失或为空时抛异常。"""
    skill_path = BASE_PATH / skill_name
    full_cmd = f"{cmd} -a claude-code --copy --yes"
    logger.debug("CMD: %s", full_cmd)
    logger.info("开始安装 skill: %s -> %s", skill_name, skill_path)
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            shlex.split(full_cmd),
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        logger.error("安装失败, 返回码=%d\nstdout:\n%s\nstderr:\n%s", e.returncode, e.stdout, e.stderr)
        raise
    except subprocess.TimeoutExpired:
        logger.error("安装超时 (%ds): %s", INSTALL_TIMEOUT, full_cmd)
        raise

    logger.info("安装完成, 耗时 %.2fs", time.perf_counter() - start)
    logger.debug("安装 stdout:\n%s\nstderr:\n%s", proc.stdout, proc.stderr)

    skill_md = skill_path / "SKILL.md"
    if not (skill_md.is_file() and skill_md.stat().st_size > 0):
        raise ValueError(f"SKILL.md 不存在或为空: {skill_md}")
    return skill_path


# ---------- 构建 Skill ----------


def parse_skill_md(path: Path) -> tuple[str, str, str]:
    """返回 (name, description, 原文)。"""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path} 没有 frontmatter")

    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise ValueError(f"{path} frontmatter 不是键值对")  # noqa: TRY004

    name, description = data.get("name"), data.get("description")
    if not name or not description:
        raise ValueError(f"{path} 缺少 name 或 description")
    return str(name).strip(), str(description).strip(), text


def guess_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def build_skill(args: Args, skill_path: Path, install_count: int) -> Skill:
    name, description, skill_md_text = parse_skill_md(skill_path / "SKILL.md")
    skill = Skill(
        id=str(uuid.uuid7()),
        display_name=args.display_name,
        name=name,
        description=description,
        skill_md_text=skill_md_text,
        install_count=install_count,
        example_prompts=args.example_prompts,
        tags=args.tags,
        root=skill_path,
        files=sorted(p for p in skill_path.rglob("*") if p.is_file()),
        author=args.author,
    )
    logger.info("构建 Skill: id=%s, name=%s, 文件 %d 个", skill.id, skill.name, len(skill.files))
    return skill


# ---------- 数据库 ----------


def insert_skill(cursor: psycopg.Cursor, skill: Skill) -> None:
    cursor.execute(
        "insert into skills (id, display_name, name, description, skill_md_text, cover_key, example_prompts, use_count, author) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            skill.id,
            skill.display_name,
            skill.name,
            skill.description,
            skill.skill_md_text,
            skill.cover_key,
            skill.example_prompts,
            skill.install_count,
            skill.author,
        ),
    )
    logger.info("skills 表插入完成")


def link_categories(cursor: psycopg.Cursor, skill: Skill) -> None:
    cursor.execute("select id, slug from skill_categories")
    slug_to_id = {slug: id_ for id_, slug in cursor.fetchall()}

    missing = [t for t in skill.tags if t not in slug_to_id]
    if missing:
        raise ValueError(f"分类不存在: {missing} (可用: {sorted(slug_to_id)})")

    cursor.executemany(
        "insert into skill_category_links (skill_id, category_id, is_primary) values (%s, %s, %s)",
        [(skill.id, slug_to_id[slug], i == 0) for i, slug in enumerate(skill.tags)],
    )
    logger.info("分类关联写入完成: %s (主分类=%s)", skill.tags, skill.tags[0])


def insert_files(cursor: psycopg.Cursor, skill: Skill) -> None:
    cursor.executemany(
        "insert into skill_files (skill_id, path, size, mime) values (%s, %s, %s, %s)",
        [(skill.id, skill.relative(p), p.stat().st_size, guess_content_type(p)) for p in skill.files],
    )
    logger.info("skill_files 写入完成: %d 条", len(skill.files))


def save_to_db(skill: Skill) -> None:
    # 注意: 不要在日志里输出连接串, 里面含密码
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cursor:
        insert_skill(cursor, skill)
        link_categories(cursor, skill)
        insert_files(cursor, skill)


# ---------- 对象存储 ----------


def upload_files(skill: Skill) -> None:
    client = Minio(S3_ENDPOINT, access_key=S3_ACCESS_KEY, secret_key=S3_SECRET_KEY, secure=S3_SECURE)
    if not client.bucket_exists(S3_BUCKET):
        logger.info("bucket 不存在, 创建: %s", S3_BUCKET)
        client.make_bucket(S3_BUCKET)

    for p in skill.files:
        key = skill.storage_key(p)
        client.fput_object(S3_BUCKET, key, str(p.absolute()), content_type=guess_content_type(p))
        logger.debug("已上传: %s", key)
    logger.info("文件上传完成: %d 个", len(skill.files))


# ---------- 入口 ----------


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    logger.info("开始导入: url=%s, display_name=%s, tags=%s", args.url, args.display_name, args.tags)

    try:
        cmd, install_count = scrape_skill_page(args.url)
        skill_path = install_skill(cmd, parse_skill_name(cmd))
        skill = build_skill(args, skill_path, install_count)
        save_to_db(skill)
        upload_files(skill)
        logger.info("导入成功: skill_id=%s, name=%s", skill.id, skill.name)
    except Exception:
        logger.exception("导入失败: url=%s", args.url)
        sys.exit(1)


if __name__ == "__main__":
    main()
