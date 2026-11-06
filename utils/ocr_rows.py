"""OCR 检测框按行合并：同一行的服务名与价格保持在同一行，图片里逐字出现的声明能作为连续原文匹配。"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

ROW_OVERLAP = 0.5  # 两个框的纵向中心距不超过较高者高度的一半，视为同一行


def rows_from_detections(detections: Iterable[tuple[Sequence[Sequence[float]], str]]) -> list[str]:
    """``detections`` 为 (四角点, 文本)；返回自上而下的文本行，每行内自左向右、以空格连接。"""
    boxes = []
    for polygon, text in detections:
        text = " ".join(str(text).split())
        if not text:
            continue
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
        boxes.append({"left": min(xs), "top": min(ys), "bottom": max(ys), "text": text})
    boxes.sort(key=lambda box: (box["top"] + box["bottom"]) / 2)
    rows: list[dict[str, Any]] = []
    for box in boxes:
        center = (box["top"] + box["bottom"]) / 2
        height = box["bottom"] - box["top"]
        if rows and abs(center - rows[-1]["center"]) <= ROW_OVERLAP * max(height, rows[-1]["height"]):
            rows[-1]["boxes"].append(box)
            rows[-1]["height"] = max(rows[-1]["height"], height)
        else:
            rows.append({"center": center, "height": height, "boxes": [box]})
    return [" ".join(box["text"] for box in sorted(row["boxes"], key=lambda box: box["left"])) for row in rows]
