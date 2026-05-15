#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🌍 谷歌卫星瓦片下载器 (单文件版) - 类型修复 + 超大范围防护版
特性: 瞬时总数计算 | 边算边下并行 | 流式分片入库 | 内存 O(1) | 实时 ETA | 404永久过滤 | 全球范围防护
依赖: pip install aiohttp rich pyyaml
注意: 仅限个人学习/科研使用，遵守 Google Maps 服务条款
"""

import asyncio
import math
import os
import signal
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Generator

import aiohttp
import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Confirm, Prompt, IntPrompt
from rich.table import Table
from rich.panel import Panel

# =========================== 全局配置与信号 ===========================
STOP_FLAG = False
CONFIG_PATH = "config.yaml"
CONSOLE = Console(force_terminal=True, legacy_windows=False)


def signal_handler(sig, frame):
    global STOP_FLAG
    CONSOLE.print("\n[yellow]⚠️  收到中断信号，正在保存进度并安全退出...[/yellow]")
    STOP_FLAG = True


signal.signal(signal.SIGINT, signal_handler)


# =========================== 合规提示 ===========================
def show_compliance_notice() -> bool:
    notice = Panel.fit(
        "⚠️  法律合规提示 ⚠️\n\n"
        "本工具仅限用于：\n"
        "• 个人学习、研究或教学目的\n"
        "• 已获得 Google 官方书面授权的场景\n"
        "• 下载公开授权或无版权限制的地理数据\n\n"
        "禁止用于：\n"
        "• 商业盈利、批量缓存、反向工程等行为\n"
        "• 违反 Google Maps 服务条款的任何用途\n\n"
        "继续使用即表示您已阅读并同意上述条款。",
        title="🔒 使用前必读",
        border_style="red"
    )
    CONSOLE.print(notice)
    return Confirm.ask("\n是否确认继续？(y/N)", default=False)


# =========================== 工具函数 ===========================
def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float('inf'): return "⏱ 计算中..."
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0: return f"⏱ {h}时{m}分{s}秒"
    if m > 0: return f"⏱ {m}分{s}秒"
    return f"⏱ {s}秒"


# =========================== 配置管理 ===========================
@dataclass
class Config:
    default_output_dir: str
    concurrency: int
    map_type: str
    max_retries: int
    skip_existing: bool
    proxy: Optional[str]
    timeout: int
    headers: dict
    db_path: str
    chunk_size: int
    fetch_batch_size: int
    max_tiles_warning: int  # ✅ 新增：瓦片数量预警阈值


def load_config(path: str = CONFIG_PATH) -> Config:
    defaults = {
        "download": {
            "output_dir": "./google_tiles", "concurrency": 8, "map_type": "s",
            "max_retries": 3, "skip_existing": True, "chunk_size": 3000, "fetch_batch_size": 400,
            "max_tiles_warning": 10_000_000  # ✅ 默认预警阈值：1000 万
        },
        "network": {
            "proxy": None, "timeout": 15,
            "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": "https://www.google.com/maps", "Accept-Language": "zh-CN,zh;q=0.9"}
        },
        "db": {"path": "./tiles.db"}
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for section in defaults:
            if section not in user_cfg:
                user_cfg[section] = defaults[section]
            else:
                for key in defaults[section]:
                    if key not in user_cfg[section]: user_cfg[section][key] = defaults[section][key]

        return Config(
            default_output_dir=user_cfg["download"]["output_dir"],
            concurrency=user_cfg["download"]["concurrency"],
            map_type=user_cfg["download"]["map_type"],
            max_retries=user_cfg["download"]["max_retries"],
            skip_existing=user_cfg["download"]["skip_existing"],
            proxy=user_cfg["network"]["proxy"],
            timeout=user_cfg["network"]["timeout"],
            headers=user_cfg["network"]["headers"],
            db_path=user_cfg["db"]["path"],
            chunk_size=int(user_cfg["download"]["chunk_size"]),
            fetch_batch_size=int(user_cfg["download"]["fetch_batch_size"]),
            max_tiles_warning=int(user_cfg["download"].get("max_tiles_warning", 10_000_000))
        )
    except FileNotFoundError:
        CONSOLE.print(f"[yellow]⚠️  未找到 {path}，使用默认配置[/yellow]")
        return Config(
            default_output_dir=defaults["download"]["output_dir"],
            **{k: v for s in defaults.values() for k, v in s.items() if
               k not in ["output_dir", "chunk_size", "fetch_batch_size", "max_tiles_warning"]},
            chunk_size=defaults["download"]["chunk_size"],
            fetch_batch_size=defaults["download"]["fetch_batch_size"],
            max_tiles_warning=defaults["download"]["max_tiles_warning"]
        )
    except Exception as e:
        CONSOLE.print(f"[red]❌ 配置加载失败: {e}[/red]")
        sys.exit(1)


# =========================== 坐标与瓦片计算 ===========================
def deg2num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    """✅ 修复：确保返回值严格为 int，避免 range() 报错"""
    n = 1 << zoom  # ✅ 用位运算替代 2.0**zoom，确保整数且更快
    xtile = int(math.floor((lon_deg + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat_deg)
    ytile = int(math.floor((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n))
    # ✅ 显式转为 int，避免 min/max 返回 float
    return int(max(0, min(xtile, n - 1))), int(max(0, min(ytile, n - 1)))


def calc_tile_count(min_lat: float, min_lon: float, max_lat: float, max_lon: float, levels: List[int]) -> int:
    total = 0
    for z in levels:
        x_min, y_max = deg2num(min_lat, min_lon, z)
        x_max, y_min = deg2num(max_lat, max_lon, z)
        # ✅ 防御：确保范围有效
        if x_max >= x_min and y_max >= y_min:
            total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total


def tile_chunk_generator(min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                         levels: List[int], chunk_size: int = 3000) -> Generator[List[Tuple[int, int, int]], None, None]:
    current_chunk = []
    for z in levels:
        x_min, y_max = deg2num(min_lat, min_lon, z)
        x_max, y_min = deg2num(max_lat, max_lon, z)
        # ✅ 防御：跳过无效范围
        if x_max < x_min or y_max < y_min: continue
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                current_chunk.append((z, x, y))
                if len(current_chunk) >= chunk_size:
                    yield current_chunk
                    current_chunk.clear()
    if current_chunk:
        yield current_chunk


# =========================== SQLite 数据库管理 ===========================
class DBManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.executescript("""
                             CREATE TABLE IF NOT EXISTS tasks
                             (
                                 id
                                 INTEGER
                                 PRIMARY
                                 KEY
                                 AUTOINCREMENT,
                                 kml_path
                                 TEXT
                                 NOT
                                 NULL,
                                 bbox
                                 TEXT
                                 NOT
                                 NULL,
                                 levels
                                 TEXT
                                 NOT
                                 NULL,
                                 output_dir
                                 TEXT
                                 NOT
                                 NULL,
                                 map_type
                                 TEXT
                                 DEFAULT
                                 's',
                                 created_at
                                 TEXT
                                 DEFAULT
                                 CURRENT_TIMESTAMP,
                                 status
                                 TEXT
                                 DEFAULT
                                 'pending'
                             );
                             CREATE TABLE IF NOT EXISTS tiles
                             (
                                 id
                                 INTEGER
                                 PRIMARY
                                 KEY
                                 AUTOINCREMENT,
                                 task_id
                                 INTEGER
                                 NOT
                                 NULL,
                                 z
                                 INTEGER
                                 NOT
                                 NULL,
                                 x
                                 INTEGER
                                 NOT
                                 NULL,
                                 y
                                 INTEGER
                                 NOT
                                 NULL,
                                 status
                                 INTEGER
                                 DEFAULT
                                 1,
                                 fail_reason
                                 TEXT,
                                 retry_count
                                 INTEGER
                                 DEFAULT
                                 0,
                                 max_retries
                                 INTEGER
                                 DEFAULT
                                 3,
                                 updated_at
                                 TEXT
                                 DEFAULT
                                 CURRENT_TIMESTAMP,
                                 UNIQUE
                             (
                                 task_id,
                                 z,
                                 x,
                                 y
                             )
                                 );
                             CREATE INDEX IF NOT EXISTS idx_tiles_task_status ON tiles(task_id, status);
                             CREATE INDEX IF NOT EXISTS idx_tiles_xyz ON tiles(z, x, y);
                             """)
        conn.commit()
        conn.close()

    def create_task(self, kml_path: str, bbox: str, levels: List[int], output_dir: str, map_type: str = 's') -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (kml_path, bbox, levels, output_dir, map_type, status) VALUES (?, ?, ?, ?, ?, 'running')",
                       (kml_path, bbox, ",".join(map(str, levels)), output_dir, map_type))
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return task_id

    def get_task_by_id(self, task_id: int) -> Optional[dict]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id, kml_path, output_dir, status FROM tasks WHERE id=?", (task_id,))
        row = cursor.fetchone()
        conn.close()
        return {"id": row[0], "kml_path": row[1], "output_dir": row[2], "status": row[3]} if row else None

    def get_all_tasks(self) -> List[dict]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT id, kml_path, output_dir, created_at, status FROM tasks ORDER BY created_at DESC")
        rows = cursor.fetchall()
        conn.close()
        return [{"id": r[0], "kml_path": r[1], "output_dir": r[2], "created_at": r[3], "status": r[4]} for r in rows]

    def insert_tile_chunk(self, task_id: int, chunk: List[Tuple[int, int, int]], max_retries: int):
        conn = self._get_conn()
        cursor = conn.cursor()
        data = [(task_id, z, x, y, 1, None, 0, max_retries) for (z, x, y) in chunk]
        cursor.executemany(
            "INSERT OR IGNORE INTO tiles (task_id, z, x, y, status, fail_reason, retry_count, max_retries) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            data
        )
        conn.commit()
        conn.close()

    def fetch_pending_batch(self, task_id: int, skip_existing: bool, limit: int = 400) -> List[Tuple[int, int, int, str]]:
        task = self.get_task_by_id(task_id)
        if not task: return []
        output_dir = task["output_dir"]
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT z, x, y FROM tiles WHERE task_id=? AND status IN (1, 3) LIMIT ?", (task_id, limit))
        rows = cursor.fetchall()
        conn.close()

        result = []
        for z, x, y in rows:
            fp = os.path.join(output_dir, str(z), str(x), f"{y}.png")
            if skip_existing and os.path.exists(fp):
                self.update_tile_status(task_id, z, x, y, 2)
            else:
                result.append((z, x, y, fp))
        return result

    def get_pending_count(self, task_id: int) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tiles WHERE task_id=? AND status IN (1, 3)", (task_id,))
        res = cursor.fetchone()
        conn.close()
        return res[0] if res else 0

    def update_tile_status(self, task_id: int, z: int, x: int, y: int, status: int, fail_reason: Optional[str] = None):
        conn = self._get_conn()
        cursor = conn.cursor()
        if status == 2:
            cursor.execute("UPDATE tiles SET status=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND z=? AND x=? AND y=?",
                           (status, task_id, z, x, y))
        elif status == 4:
            cursor.execute("UPDATE tiles SET status=?, fail_reason=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND z=? AND x=? AND y=?",
                           (status, fail_reason, task_id, z, x, y))
        elif status == 3:
            cursor.execute(
                "UPDATE tiles SET status=?, fail_reason=?, retry_count=retry_count+1, updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND z=? AND x=? AND y=?",
                (status, fail_reason, task_id, z, x, y))
        conn.commit()
        conn.close()

    def get_task_stats(self, task_id: int) -> dict:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT COUNT(*),
                              SUM(CASE WHEN status = 2 THEN 1 ELSE 0 END),
                              SUM(CASE WHEN status = 3 THEN 1 ELSE 0 END),
                              SUM(CASE WHEN status = 4 THEN 1 ELSE 0 END)
                       FROM tiles
                       WHERE task_id = ?
                       """, (task_id,))
        row = cursor.fetchone()
        conn.close()
        return {
            "total": row[0] or 0,
            "success": row[1] or 0,
            "failed": row[2] or 0,
            "skipped": row[3] or 0
        }

    def update_task_status(self, task_id: int, status: str):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        conn.commit()
        conn.close()


# =========================== KML 解析器 ===========================
def parse_kml_bbox(kml_path: str) -> Tuple[float, float, float, float]:
    tree = ET.parse(kml_path)
    root = tree.getroot()
    coordinates = []
    for elem in root.iter():
        if elem.tag.endswith('coordinates') and elem.text:
            for coord_str in elem.text.strip().split():
                parts = coord_str.split(',')
                if len(parts) >= 2:
                    try:
                        lon, lat = float(parts[0]), float(parts[1])
                        if -180 <= lon <= 180 and -90 <= lat <= 90: coordinates.append((lon, lat))
                    except ValueError:
                        continue
    if not coordinates: raise ValueError("KML 文件中未找到有效坐标")
    lons, lats = [c[0] for c in coordinates], [c[1] for c in coordinates]
    return min(lats), min(lons), max(lats), max(lons)


# =========================== 异步下载器 ===========================
async def download_tile(session: aiohttp.ClientSession, url: str, file_path: str, headers: dict, proxy: Optional[str], timeout: int) -> \
Tuple[bool, Optional[str], int]:
    try:
        async with session.get(url, headers=headers, proxy=proxy, timeout=timeout) as resp:
            if resp.status == 200:
                content = await resp.read()
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'wb') as f: f.write(content)
                return True, None, len(content)
            return False, f"HTTP {resp.status}", 0
    except asyncio.TimeoutError:
        return False, "网络超时", 0
    except aiohttp.ClientError as e:
        return False, f"其他错误: {str(e)[:80]}", 0
    except Exception as e:
        return False, f"其他错误: {str(e)[:80]}", 0


# =========================== TUI 主界面 ===========================
def show_main_menu() -> str:
    table = Table(title="🌍 谷歌卫星瓦片下载器 v3.4", show_header=False, box=None)
    table.add_column("选项", style="cyan")
    table.add_row("[1] 🆕 新建下载任务")
    table.add_row("[2] 📋 管理/恢复历史任务")
    table.add_row("[3] 🚪 退出程序")
    CONSOLE.print(table)
    return Prompt.ask("请选择 [1-3]", choices=["1", "2", "3"], default="1")


def prompt_new_task(default_output_dir: str) -> Tuple[str, List[int], str]:
    kml_path = Prompt.ask("📁 请输入 KML 文件路径")
    if not os.path.exists(kml_path):
        CONSOLE.print(f"[red]❌ 文件不存在: {kml_path}[/red]")
        return prompt_new_task(default_output_dir)
    levels_str = Prompt.ask("🔢 请输入下载级别 (逗号分隔，如 10,11,12)")
    try:
        levels = [int(l.strip()) for l in levels_str.split(",") if l.strip()]
        if not all(0 <= l <= 22 for l in levels): raise ValueError
    except ValueError:
        CONSOLE.print("[red]❌ 级别格式错误，请输入 0-22 的整数[/red]")
        return prompt_new_task(default_output_dir)
    CONSOLE.print(f"[dim]💡 默认输出目录: {default_output_dir}[/dim]")
    custom_output = Prompt.ask("📂 请输入自定义输出目录 (回车使用默认)", default="").strip()
    output_dir = os.path.abspath(custom_output if custom_output else default_output_dir)
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        test = Path(output_dir) / ".test_write"
        test.touch();
        test.unlink()
    except Exception as e:
        CONSOLE.print(f"[red]❌ 输出目录不可写: {e}[/red]")
        return prompt_new_task(default_output_dir)
    return kml_path, sorted(levels), output_dir


def run_concurrent_download(task_id: int, min_lat: float, min_lon: float, max_lat: float, max_lon: float,
                            levels: List[int], config: Config, db: DBManager):
    total_tiles = calc_tile_count(min_lat, min_lon, max_lat, max_lon, levels)
    producer_done = asyncio.Event()
    stats = {"success": 0, "failed": 0, "skipped": 0, "bytes": 0}
    lock = asyncio.Lock()
    url_template = f"https://mt0.google.com/vt/lyrs={config.map_type}&x={{x}}&y={{y}}&z={{z}}"
    start_time = time.time()

    async def producer():
        try:
            for chunk in tile_chunk_generator(min_lat, min_lon, max_lat, max_lon, levels, chunk_size=config.chunk_size):
                if STOP_FLAG: break
                await asyncio.to_thread(db.insert_tile_chunk, task_id, chunk, config.max_retries)
                await asyncio.sleep(0)
        finally:
            producer_done.set()

    async def consumer():
        nonlocal stats, start_time
        semaphore = asyncio.Semaphore(config.concurrency)

        with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                BarColumn(), TaskProgressColumn(), TextColumn("• {task.fields[stats]}"),
                console=CONSOLE, transient=False
        ) as progress:
            main_task = progress.add_task("[cyan]边算边下中...", total=total_tiles, stats="0/0/0/0 | ⚡ 0 KB/s")

            async with aiohttp.ClientSession(headers=config.headers) as session:
                while not STOP_FLAG:
                    if producer_done.is_set() and await asyncio.to_thread(db.get_pending_count, task_id) == 0:
                        break

                    batch = await asyncio.to_thread(db.fetch_pending_batch, task_id, config.skip_existing, limit=config.fetch_batch_size)
                    if not batch:
                        await asyncio.sleep(0.2)
                        continue

                    async def worker(z, x, y, fp):
                        nonlocal stats
                        async with semaphore:
                            if config.skip_existing and os.path.exists(fp):
                                await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 2)
                                async with lock: stats["success"] += 1
                                return

                            url = url_template.format(z=z, x=x, y=y)
                            success, err, size = await download_tile(session, url, fp, config.headers, config.proxy, config.timeout)
                            async with lock:
                                if success:
                                    stats["success"] += 1
                                    stats["bytes"] += size
                                    await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 2)
                                else:
                                    if "404" in str(err):
                                        stats["skipped"] += 1
                                        await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 4, err)
                                    else:
                                        stats["failed"] += 1
                                        await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 3, err)

                    tasks = [asyncio.create_task(worker(z, x, y, fp)) for z, x, y, fp in batch]
                    for f in asyncio.as_completed(tasks):
                        await f
                        done = stats["success"] + stats["skipped"]
                        processed = done + stats["failed"]
                        remaining = total_tiles - processed
                        elapsed = max(time.time() - start_time, 0.01)

                        speed = stats["bytes"] / elapsed
                        speed_str = f"{speed / (1024 * 1024):.2f} MB/s" if speed > 1024 * 1024 else f"{speed / 1024:.1f} KB/s"

                        if processed > 0 and remaining > 0:
                            eta_seconds = (elapsed / processed) * remaining
                            eta_str = format_eta(eta_seconds)
                        else:
                            eta_str = "⏱ --:--:--"

                        stats_text = f"{stats['success']}✅/{stats['failed']}❌/{stats['skipped']}⏭️/{remaining}⏳ | ⚡ {speed_str} | {eta_str}"
                        progress.update(main_task, completed=done, stats=stats_text)
                        progress.refresh()

    async def run_all():
        await asyncio.gather(producer(), consumer())

    CONSOLE.print(f"[dim]🚀 已启动边算边下模式 (入库块={config.chunk_size}, 拉取批={config.fetch_batch_size})...[/dim]")
    try:
        asyncio.run(run_all())
    except Exception as e:
        CONSOLE.print(f"[red]💥 下载过程发生异常: {e}[/red]")


def list_and_select_task(db: DBManager) -> Optional[int]:
    tasks = db.get_all_tasks()
    if not tasks:
        CONSOLE.print("[yellow]📭 暂无任务记录[/yellow]")
        return None
    table = Table(title="📋 历史任务列表")
    table.add_column("ID", style="dim", justify="center")
    table.add_column("KML 文件")
    table.add_column("输出目录")
    table.add_column("创建时间")
    table.add_column("状态", justify="center")
    for t in tasks:
        emoji = {"pending": "⏳", "running": "🔄", "paused": "⏸️", "completed": "✅"}.get(t["status"], "❓")
        table.add_row(str(t["id"]), Path(t["kml_path"]).name, Path(t["output_dir"]).name, t["created_at"], f"{emoji} {t['status']}")
    CONSOLE.print(table)
    CONSOLE.print("[dim]💡 输入任务ID恢复下载，输入 0 返回主菜单[/dim]")
    choice = IntPrompt.ask("🔢 请输入任务ID")
    if choice == 0: return None
    task_info = db.get_task_by_id(choice)
    if not task_info:
        CONSOLE.print(f"[red]❌ 任务 {choice} 不存在[/red]")
        return list_and_select_task(db)
    if task_info["status"] == "completed":
        CONSOLE.print("[yellow]⚠️  该任务已完成，无需恢复[/yellow]")
        return list_and_select_task(db)
    return choice


def resume_task_flow(task_id: int, config: Config, db: DBManager):
    task = db.get_task_by_id(task_id)
    if not task: return
    CONSOLE.print(f"[cyan]🔄 正在恢复任务 #{task_id}...[/cyan]")
    db.update_task_status(task_id, "running")

    pending = db.get_pending_count(task_id)
    total_tiles = db.get_task_stats(task_id)['total']
    if pending == 0:
        CONSOLE.print("[green]✨ 任务已完成或无有效待下载瓦片[/green]")
        db.update_task_status(task_id, "completed")
        return

    CONSOLE.print(f"[cyan]📦 发现 {pending} 个待处理瓦片 (总计 {total_tiles})，开始继续...[/cyan]")

    hist_stats = db.get_task_stats(task_id)
    stats = {"success": hist_stats['success'], "failed": 0, "skipped": hist_stats['skipped'], "bytes": 0}
    lock = asyncio.Lock()
    start_time = time.time()
    url_template = f"https://mt0.google.com/vt/lyrs={config.map_type}&x={{x}}&y={{y}}&z={{z}}"
    initial_done = stats["success"] + stats["skipped"]

    async def consumer_only():
        semaphore = asyncio.Semaphore(config.concurrency)
        with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                BarColumn(), TaskProgressColumn(), TextColumn("• {task.fields[stats]}"),
                console=CONSOLE, transient=False
        ) as progress:
            main_task = progress.add_task("[cyan]恢复下载中...", total=total_tiles, completed=initial_done, stats="0/0/0/0 | ⚡ 0 KB/s")
            async with aiohttp.ClientSession(headers=config.headers) as session:
                while not STOP_FLAG:
                    batch = await asyncio.to_thread(db.fetch_pending_batch, task_id, config.skip_existing, limit=config.fetch_batch_size)
                    if not batch: break

                    async def worker(z, x, y, fp):
                        nonlocal stats
                        async with semaphore:
                            if config.skip_existing and os.path.exists(fp):
                                await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 2)
                                async with lock: stats["success"] += 1
                                return
                            url = url_template.format(z=z, x=x, y=y)
                            success, err, size = await download_tile(session, url, fp, config.headers, config.proxy, config.timeout)
                            async with lock:
                                if success:
                                    stats["success"] += 1
                                    stats["bytes"] += size
                                    await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 2)
                                else:
                                    if "404" in str(err):
                                        stats["skipped"] += 1
                                        await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 4, err)
                                    else:
                                        stats["failed"] += 1
                                        await asyncio.to_thread(db.update_tile_status, task_id, z, x, y, 3, err)

                    tasks = [asyncio.create_task(worker(z, x, y, fp)) for z, x, y, fp in batch]
                    for f in asyncio.as_completed(tasks):
                        await f
                        done = stats["success"] + stats["skipped"]
                        processed = done + stats["failed"]
                        remaining = total_tiles - processed
                        elapsed = max(time.time() - start_time, 0.01)

                        speed = stats["bytes"] / elapsed if elapsed > 0 else 0
                        speed_str = f"{speed / (1024 * 1024):.2f} MB/s" if speed > 1024 * 1024 else f"{speed / 1024:.1f} KB/s"

                        if processed > 0 and remaining > 0:
                            eta_seconds = (elapsed / processed) * remaining
                            eta_str = format_eta(eta_seconds)
                        else:
                            eta_str = "⏱ --:--:--"

                        stats_text = f"{stats['success']}✅/{stats['failed']}❌/{stats['skipped']}⏭️/{remaining}⏳ | ⚡ {speed_str} | {eta_str}"
                        progress.update(main_task, completed=done, stats=stats_text)

    try:
        asyncio.run(consumer_only())
    except Exception as e:
        CONSOLE.print(f"[red]💥 恢复过程异常: {e}[/red]")

    if STOP_FLAG:
        db.update_task_status(task_id, "paused")
        CONSOLE.print("[yellow]⏸️  任务已暂停[/yellow]")
    else:
        final = db.get_task_stats(task_id)
        if final['success'] + final['skipped'] == final['total'] and final['failed'] == 0:
            db.update_task_status(task_id, "completed")
            CONSOLE.print("[green]🎉 任务完成！[/green]")
        else:
            db.update_task_status(task_id, "paused")
            CONSOLE.print("[yellow]📊 任务部分完成[/yellow]")


# =========================== 主程序入口 ===========================
def main():
    if not show_compliance_notice():
        CONSOLE.print("[yellow]👋 已退出，感谢使用[/yellow]")
        return
    config = load_config()
    db = DBManager(config.db_path)
    CONSOLE.print(Panel(
        f"⚙️  配置已加载\n• 默认输出: {config.default_output_dir}\n• 并发数: {config.concurrency}\n• 入库块: {config.chunk_size} | 拉取批: {config.fetch_batch_size}\n• 瓦片预警阈值: {config.max_tiles_warning:,}",
        title="🔧 初始化完成", border_style="green"))

    global STOP_FLAG
    while not STOP_FLAG:
        choice = show_main_menu()
        if choice == "1":
            kml_path, levels, output_dir = prompt_new_task(config.default_output_dir)
            try:
                min_lat, min_lon, max_lat, max_lon = parse_kml_bbox(kml_path)
                total = calc_tile_count(min_lat, min_lon, max_lat, max_lon, levels)
                CONSOLE.print(f"[green]✅ 解析成功: BBox=[{min_lat},{min_lon},{max_lat},{max_lon}] 级别={levels}[/green]")

                # ✅ 超大范围预警
                if total > config.max_tiles_warning:
                    CONSOLE.print(Panel(
                        f"[red]⚠️  瓦片数量预警[/red]\n"
                        f"• 预估总数: [bold yellow]{total:,}[/bold yellow]\n"
                        f"• 预警阈值: {config.max_tiles_warning:,}\n"
                        f"• 建议操作:\n"
                        f"  1. 缩小 KML 边界范围\n"
                        f"  2. 降低下载级别（如只选 10-14 级）\n"
                        f"  3. 修改 config.yaml 中 max_tiles_warning 值（需谨慎）",
                        title="🚨 高风险任务",
                        border_style="red"
                    ))
                    if not Confirm.ask("❓ 是否仍要创建此任务？(y/N)", default=False):
                        CONSOLE.print("[yellow]👋 已取消任务创建[/yellow]")
                        continue

                CONSOLE.print(f"[cyan]📦 预计瓦片总数: {total:,} → 输出: {output_dir}[/cyan]")
            except Exception as e:
                CONSOLE.print(f"[red]❌ KML 解析失败: {e}[/red]")
                continue

            task_id = db.create_task(kml_path, f"{min_lat},{min_lon},{max_lat},{max_lon}", levels, output_dir, config.map_type)
            CONSOLE.print(f"[cyan]🚀 启动边算边下模式 (共 {total:,} 瓦片)...[/cyan]")
            run_concurrent_download(task_id, min_lat, min_lon, max_lat, max_lon, levels, config, db)

            if STOP_FLAG:
                db.update_task_status(task_id, "paused")
                CONSOLE.print("[yellow]⏸️  任务已暂停[/yellow]")
            else:
                final = db.get_task_stats(task_id)
                if final['success'] + final['skipped'] == final['total'] and final['failed'] == 0:
                    db.update_task_status(task_id, "completed")
                    CONSOLE.print("[green]🎉 任务完成！[/green]")
                else:
                    db.update_task_status(task_id, "paused")
                    CONSOLE.print("[yellow]📊 任务部分完成，可在任务管理中继续[/yellow]")

        elif choice == "2":
            task_id = list_and_select_task(db)
            if task_id: resume_task_flow(task_id, config, db)
        elif choice == "3":
            break
        if not STOP_FLAG: CONSOLE.print()


if __name__ == "__main__":
    main()