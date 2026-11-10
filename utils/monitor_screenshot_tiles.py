from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image

from utils.monitor_image_evidence import pixel_digest, tile_ranges
from utils.monitor_price_store import atomic_bytes


def _tile(path: str, data: bytes) -> dict:
    return {"path": path, "bytes": len(data), "png_digest": hashlib.sha256(data).hexdigest()}


def variant_tiles(path: str) -> tuple[dict, dict]:
    """商品选项只截一次图，a、b 两轮读取同一张截图。"""
    data = Path(path).read_bytes()
    tile = _tile(path, data)
    return {**tile, "pixel_digest": pixel_digest(data)}, tile


def page_screenshot(png: bytes, directory: Path, *, tiles: bool) -> dict:
    """保存整页截图；页面需要读图时再从这张截图裁切两轮图块。"""
    path = directory / "screenshot.png"
    atomic_bytes(path, png)
    return {"screenshot_path": str(path), "image_tiles": screenshot_tiles(png, directory) if tiles else {"a": [], "b": []}}


def screenshot_tiles(png: bytes, directory: Path) -> dict:
    """按页面坐标裁切整页截图：a 轮用常规边界，b 轮用错开的边界，两轮都保持原尺寸。"""
    full = Image.open(io.BytesIO(png))
    tiles = {"a": [], "b": []}
    for round_name in ("a", "b"):
        for index, (y, height) in enumerate(tile_ranges(full.height, shifted=round_name == "b")):
            buffer = io.BytesIO()
            full.crop((0, y, full.width, y + height)).save(buffer, format="PNG")
            data = buffer.getvalue()
            path = directory / f"image_{round_name}_{index:03d}.png"
            atomic_bytes(path, data)
            tile = {**_tile(str(path), data), "clip": {"x": 0, "y": y, "width": full.width, "height": height, "scale": 1}}
            if round_name == "a":
                tile["pixel_digest"] = pixel_digest(data)
            tiles[round_name].append(tile)
    return tiles
