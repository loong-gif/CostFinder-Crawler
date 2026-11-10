import hashlib
import io
import random
from pathlib import Path

from PIL import Image

from utils.monitor_image_evidence import pixel_digest, tile_ranges
from utils.monitor_screenshot_tiles import page_screenshot, screenshot_tiles, variant_tiles


def _screenshot(width=200, height=3700):
    # 每个像素随机取值，裁切偏移一行都会让像素比较失败
    image = Image.frombytes("RGB", (width, height), random.Random(7).randbytes(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return image, buffer.getvalue()


def test_round_a_tiles_are_regions_of_the_single_screenshot(tmp_path):
    full, png = _screenshot()
    tiles = screenshot_tiles(png, tmp_path)
    assert [(t["clip"]["y"], t["clip"]["height"]) for t in tiles["a"]] == tile_ranges(full.height)
    for index, tile in enumerate(tiles["a"]):
        clip = tile["clip"]
        data = Path(tile["path"]).read_bytes()
        assert tile["path"] == str(tmp_path / f"image_a_{index:03d}.png")
        assert clip == {"x": 0, "y": clip["y"], "width": 200, "height": clip["height"], "scale": 1}
        assert Image.open(io.BytesIO(data)).tobytes() == full.crop((0, clip["y"], 200, clip["y"] + clip["height"])).tobytes()
        assert tile["bytes"] == len(data) and tile["png_digest"] == hashlib.sha256(data).hexdigest()
        assert tile["pixel_digest"] == pixel_digest(data)


def test_round_b_tiles_are_native_size_regions_at_shifted_bounds(tmp_path):
    full, png = _screenshot()
    tiles = screenshot_tiles(png, tmp_path)
    assert [(t["clip"]["y"], t["clip"]["height"]) for t in tiles["b"]] == tile_ranges(full.height, shifted=True)
    assert tile_ranges(full.height, shifted=True) != tile_ranges(full.height)
    for index, tile in enumerate(tiles["b"]):
        clip = tile["clip"]
        data = Path(tile["path"]).read_bytes()
        image = Image.open(io.BytesIO(data))
        assert tile["path"] == str(tmp_path / f"image_b_{index:03d}.png")
        assert clip == {"x": 0, "y": clip["y"], "width": 200, "height": clip["height"], "scale": 1}
        assert image.size == (200, clip["height"])
        assert image.tobytes() == full.crop((0, clip["y"], 200, clip["y"] + clip["height"])).tobytes()
        assert tile["bytes"] == len(data) and tile["png_digest"] == hashlib.sha256(data).hexdigest()
        assert "pixel_digest" not in tile


def test_variant_screenshot_is_read_in_both_rounds(tmp_path):
    _, png = _screenshot(120, 90)
    path = tmp_path / "variant_3.png"
    path.write_bytes(png)
    first, second = variant_tiles(str(path))
    assert second == {"path": str(path), "bytes": len(png), "png_digest": hashlib.sha256(png).hexdigest()}
    assert first == {**second, "pixel_digest": pixel_digest(png)}


def test_page_screenshot_is_saved_and_cut_into_tiles_only_when_images_are_read(tmp_path):
    full, png = _screenshot()
    shot = page_screenshot(png, tmp_path / "plain", tiles=False)
    assert shot == {"screenshot_path": str(tmp_path / "plain/screenshot.png"), "image_tiles": {"a": [], "b": []}}
    assert (tmp_path / "plain/screenshot.png").read_bytes() == png
    shot = page_screenshot(png, tmp_path / "images", tiles=True)
    assert shot["screenshot_path"] == str(tmp_path / "images/screenshot.png")
    assert shot["image_tiles"] == screenshot_tiles(png, tmp_path / "images")
    assert len(shot["image_tiles"]["a"]) == len(tile_ranges(full.height))
