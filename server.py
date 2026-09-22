#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""白泽评审 Baize Review · 壁画修复候选评分工作台 —— 本地服务

白泽是《山海经》里知万物、能辨优劣的神兽；本工具替人做的正是这件事：
把原图与若干张 AI 修复候选图放在一起，用可解释的轻量指标算出两张榜——
「保守修复榜」与「展示复原榜」。

本文件分三层：

  1. 算法层（mask 生成、候选打分、指标计算、两张榜的排序与解释）
     —— 与旧版 `mural-review-tool/server.py` **逐行一致**，没有做任何数学改动。
        打分公式、权重、阈值、指标定义、mask 合成规则全部原样保留。
  2. 业务层（上传、建项目、尺寸对齐、人工修正、评分、导出）
     —— 逻辑不变，只把错误处理换成统一的 `toolkit_core` 错误结构，
        并补上参数上限校验（SERIES-SPEC §7 / S4、S5）。
  3. HTTP 层
     —— 统一走 `toolkit_core`：安全路径解析、受限 CORS、分块传输、统一 JSON。

启动：``python server.py [端口]``，默认 8766（SERIES-SPEC §2 写死）。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np
from PIL import Image, ImageDraw

import toolkit_core as core

# --------------------------------------------------------------------------
# 路径与版本
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
ASSET_ROOT = ROOT / "assets"
RUNS_DIR = ROOT / "runs"
UPLOADS_DIR = ROOT / "uploads"
PREVIEW_DIR = RUNS_DIR / "_previews"
HISTORY_PATH = ROOT / "history.json"

APP_SLUG = "baize-review"
APP_NAME = "白泽评审"
APP_NAME_EN = "Baize Review"
#: 版本号唯一来源（SERIES-SPEC §3）。README / CHANGELOG / 页面页头必须与它一致。
APP_VERSION = "1.1.0"

DEFAULT_PORT = core.PORT_MAP[APP_SLUG]  # 8766

#: 打分算法版本。这不是工具版本号，是"这套公式"的版本，供校准归档使用。
SCORING_VERSION = "v2.1-lightweight-calibration"

TARGET_LONG_EDGE = 2048
RUNS_MAX_AGE_DAYS = 7

# --------------------------------------------------------------------------
# 上限（SERIES-SPEC §7 / S4、S5）
# 说明：这些是"防误操作"的上限，不是业务约束，取值都留了充足余量，
#      以免正常的大幅面壁画被挡在门外。见 CHANGELOG「已知取舍」。
# --------------------------------------------------------------------------

#: 单个上传文件字节上限：1 GiB。壁画扫描件常见 100-300 MB，1 GiB 余量充足。
MAX_UPLOAD_BYTES = 1024 ** 3
#: 一次项目的候选图张数上限。
MAX_CANDIDATES = 64
#: mask 灵敏度取值范围（与页面滑杆 0.2-2 一致）。
SENSITIVITY_RANGE = (0.2, 2.0)
#: 人工修正笔数 / 总点数上限（仅防病态请求，正常涂画远达不到）。
MAX_STROKES = 20_000
MAX_STROKE_POINTS = 400_000
#: 笔刷尺寸上限（apply_overrides 原本只做 max(2, size)，这里补一个上界）。
MAX_BRUSH_SIZE = 4096
#: 人工修正请求体上限：笔迹 JSON 天然比普通表单大，所以单独放宽到 8 MB。
OVERRIDE_JSON_LIMIT = 8 * 1024 * 1024
#: 预览图长边默认值
DEFAULT_PREVIEW_MAX_SIZE = 1600

for directory in (RUNS_DIR, UPLOADS_DIR, PREVIEW_DIR):
    directory.mkdir(parents=True, exist_ok=True)

_HISTORY_LOCK = threading.Lock()


# ==========================================================================
# 旧版保留的通用小工具（行为不变）
# ==========================================================================


def cleanup_old_runs() -> int:
    """清理超过 RUNS_MAX_AGE_DAYS 的 runs/ 与 uploads/ 子目录（保留 `_` 开头）。"""
    cutoff = time.time() - RUNS_MAX_AGE_DAYS * 24 * 60 * 60
    removed = 0
    for base in (RUNS_DIR, UPLOADS_DIR):
        try:
            for entry in base.iterdir():
                if entry.name.startswith("_"):
                    continue
                if not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                        removed += 1
                except Exception:
                    pass
        except Exception:
            pass
    return removed


cleanup_old_runs()


def load_auto_mask_module():
    """把同目录下的 auto_mask.py 当模块加载，保证单仓库克隆即可运行。"""
    script_path = ROOT / "auto_mask.py"
    if not script_path.exists():
        raise RuntimeError(
            f"auto_mask.py not found inside tool directory: {script_path}. "
            "The tool must stay self-contained."
        )
    spec = importlib.util.spec_from_file_location("auto_mask_module", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load auto_mask.py from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_mask_module"] = module
    spec.loader.exec_module(module)
    return module


AUTO_MASK = load_auto_mask_module()


def safe_name(name: str) -> str:
    """旧版文件名清洗：保留中文、字母数字与 `._-()`，其余压成下划线。

    刻意保留旧实现（不改用 core.safe_filename），因为项目内候选图文件名
    `01_xxx.png` 依赖它的清洗结果，改掉会破坏已有项目的可复现性。
    """
    name = Path(name or "").name
    name = re.sub(r"[^\w.\-()\u4e00-\u9fff]+", "_", name, flags=re.UNICODE)
    return name.strip("._") or f"file_{int(time.time())}"


def powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_powershell_dialog(command: str) -> str:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, check=False,
    )
    return completed.stdout.strip()


def clean_dialog_output(value: str, suffixes: list[str] | None = None) -> str:
    raw = (value or "").strip().strip('"')
    if not raw:
        return ""
    for line in raw.splitlines():
        candidate = line.strip().strip('"')
        if candidate and Path(candidate).exists():
            return candidate
    if suffixes:
        lowered = raw.lower()
        best_end = None
        for suffix in sorted({s.lower() for s in suffixes if s}, key=len, reverse=True):
            idx = lowered.find(suffix)
            if idx >= 0:
                end = idx + len(suffix)
                if best_end is None or end < best_end:
                    best_end = end
        if best_end:
            return raw[:best_end].strip().strip('"')
    return raw


def _dialog_filter(filetypes: list[tuple[str, str]]) -> tuple[str, list[str]]:
    filters = []
    suffixes = []
    for label, pattern in filetypes:
        parts = [part for part in pattern.split() if part]
        suffixes.extend(part.replace("*", "") for part in parts if part.startswith("*."))
        filter_pattern = ";".join(parts)
        filters.append(f"{label} ({filter_pattern})|{filter_pattern}")
    return ("|".join(filters) if filters else "All files (*.*)|*.*"), suffixes


def choose_file_windows(title: str, filetypes: list[tuple[str, str]]) -> str:
    filter_text, suffixes = _dialog_filter(filetypes)
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.Form;"
        "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.WindowState='Minimized';$f.Show();"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Title={powershell_quote(title)};"
        f"$d.Filter={powershell_quote(filter_text)};"
        "$d.Multiline=$false;"
        "if($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK)"
        "{Write-Output $d.FileName};"
        "$f.Close();"
    )
    return clean_dialog_output(run_powershell_dialog(command), suffixes)


def choose_files_windows(title: str, filetypes: list[tuple[str, str]]) -> list[str]:
    filter_text, suffixes = _dialog_filter(filetypes)
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.Form;"
        "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.WindowState='Minimized';$f.Show();"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        f"$d.Title={powershell_quote(title)};"
        f"$d.Filter={powershell_quote(filter_text)};"
        "$d.Multiselect=$true;"
        "if($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK)"
        "{foreach($p in $d.FileNames){Write-Output $p}};"
        "$f.Close();"
    )
    raw = run_powershell_dialog(command)
    if not raw:
        return []
    picked = []
    for line in raw.splitlines():
        candidate = line.strip().strip('"')
        if candidate and Path(candidate).exists():
            picked.append(candidate)
    return picked or ([clean_dialog_output(raw, suffixes)] if raw.strip() else [])


def image_info(path: Path) -> dict:
    with Image.open(path) as image:
        return {
            "path": str(path),
            "name": path.name,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "format": image.format,
        }


def detect_image_extension(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            buf = handle.read(12)
        if buf[:4] == b"\x89PNG":
            return ".png"
        if buf[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
            return ".webp"
        if buf[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
            return ".tiff"
    except Exception:
        pass
    return path.suffix.lower() or ".png"


def normalize_image_extension(path: Path) -> Path:
    actual = detect_image_extension(path)
    current = path.suffix.lower()
    if not actual or actual == current:
        return path
    next_path = path.with_suffix(actual)
    try:
        path.rename(next_path)
    except Exception:
        return path
    return next_path


def preview_path_for(path: Path, max_size: int = DEFAULT_PREVIEW_MAX_SIZE) -> Path:
    key = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return PREVIEW_DIR / f"{key}_{max_size}.jpg"


def ensure_preview(path: Path, max_size: int = DEFAULT_PREVIEW_MAX_SIZE) -> Path:
    out = preview_path_for(path, max_size)
    try:
        if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
            return out
    except Exception:
        pass
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        img.save(out, "JPEG", quality=88, optimize=True)
    return out


def read_history() -> dict:
    with _HISTORY_LOCK:
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"originals": []}


def write_history(data: dict) -> None:
    with _HISTORY_LOCK:
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, HISTORY_PATH)


def add_original_history(path: Path, info: dict) -> None:
    data = read_history()
    items = data.get("originals", [])
    item = {
        "name": info.get("name", path.name),
        "path": str(path),
        "width": info.get("width"),
        "height": info.get("height"),
        "addedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = [item] + [old for old in items if old.get("path") != str(path)]
    data["originals"] = items[:80]
    write_history(data)


def url_for(path: Path) -> str:
    """把仓库内的文件路径转成 URL 路径，例如 `/runs/<id>/masks/x.png`。"""
    relative = path.resolve().relative_to(ROOT.resolve()).as_posix()
    return "/" + quote(relative)


def normalize_image_to_target(src: Path, dest: Path, target_long_edge: int = TARGET_LONG_EDGE) -> dict:
    """按长边等比对齐到 target_long_edge（两侧都不足时也放大到目标长边）。

    这一步是评分的前提：所有候选图与原图必须像素级同尺寸。
    缩放规则与旧版逐行一致，未做任何改动。
    """
    with Image.open(src) as image:
        source_width, source_height = image.size
        image = image.convert("RGB")
        if max(source_width, source_height) > target_long_edge or min(source_width, source_height) < target_long_edge // 2:
            if source_width >= source_height:
                new_w = target_long_edge
                new_h = max(1, round(source_height * target_long_edge / source_width))
            else:
                new_h = target_long_edge
                new_w = max(1, round(source_width * target_long_edge / source_height))
            image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        image.save(dest)
    info = image_info(dest)
    info["source_width"] = source_width
    info["source_height"] = source_height
    info["normalized"] = (source_width, source_height) != (info["width"], info["height"])
    info["target_width"] = info["width"]
    info["target_height"] = info["height"]
    return info


def _project_path(project_id: str) -> Path:
    """把 projectId 解析成 runs/ 下的安全路径（不检查是否存在）。越界由 core.safe_join 拦截（S1）。"""
    raw = str(project_id or "").strip()
    if not raw:
        raise core.ValidationError("缺少 projectId。", field="projectId")
    return core.safe_join(RUNS_DIR, safe_name(raw))


def project_dir(project_id: str) -> Path:
    """取一个**已存在**的项目目录；不存在返回 404。"""
    path = _project_path(project_id)
    if not path.is_dir():
        raise core.NotFoundError(f"找不到这个项目：{project_id}", detail=f"projectId={project_id!r}")
    return path


def resolve_tool_path(value: str) -> Path:
    """解析 `/api/preview?path=` 里的路径，并保证它落在仓库之内（S1）。

    历史行为：既支持仓库内的相对路径，也支持本机绝对路径（来自文件选择框）。
    这里两种都保留，但一律经过 `core.is_inside` / `core.safe_join` 的真路径校验，
    不再有任何字符串前缀式的包含判断。
    """
    raw = unquote(value or "").strip().strip('"')
    if not raw:
        raise core.ValidationError("缺少图片路径。", field="path")
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not core.is_inside(resolved, ROOT):
            raise core.PathEscapeError("请求的路径超出了允许的目录范围。", detail=f"path={raw!r}")
        return resolved
    return core.safe_join(ROOT, raw)


# ==========================================================================
# 算法层 —— 以下函数为旧版原实现，逐行保留，请勿改动数学逻辑
# ==========================================================================


def load_rgb_array(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB")).astype(np.float32) / 255.0


def load_mask(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("L")) > 127


def save_mask(mask: np.ndarray, path: Path) -> None:
    Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(path)


def luminance(arr: np.ndarray) -> np.ndarray:
    return arr[..., 0] * 0.2126 + arr[..., 1] * 0.7152 + arr[..., 2] * 0.0722


def gradient(gray: np.ndarray) -> np.ndarray:
    padded = np.pad(gray, 1, mode="edge")
    gx = (
        -padded[:-2, :-2] + padded[:-2, 2:]
        - 2 * padded[1:-1, :-2] + 2 * padded[1:-1, 2:]
        - padded[2:, :-2] + padded[2:, 2:]
    )
    gy = (
        -padded[:-2, :-2] - 2 * padded[:-2, 1:-1] - padded[:-2, 2:]
        + padded[2:, :-2] + 2 * padded[2:, 1:-1] + padded[2:, 2:]
    )
    values = np.sqrt(gx * gx + gy * gy)
    hi = np.percentile(values, 99.0)
    if hi <= 0:
        return np.zeros_like(values)
    return np.clip(values / hi, 0.0, 1.0)


def rgb_to_hsv(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    maxc = np.max(arr, axis=2)
    minc = np.min(arr, axis=2)
    delta = maxc - minc
    hue = np.zeros_like(maxc)
    mask = delta > 1e-6
    r_mask = mask & (maxc == r)
    g_mask = mask & (maxc == g)
    b_mask = mask & (maxc == b)
    hue[r_mask] = ((g[r_mask] - b[r_mask]) / delta[r_mask]) % 6.0
    hue[g_mask] = ((b[g_mask] - r[g_mask]) / delta[g_mask]) + 2.0
    hue[b_mask] = ((r[b_mask] - g[b_mask]) / delta[b_mask]) + 4.0
    hue = hue / 6.0
    saturation = np.where(maxc > 1e-6, delta / np.maximum(maxc, 1e-6), 0.0)
    return hue, saturation, maxc


def hue_distance(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    diff = np.abs(a - b)
    return np.minimum(diff, 1.0 - diff)


def block_std(values: np.ndarray, mask: np.ndarray | None = None, blocks: int = 8) -> float:
    height, width = values.shape
    samples = []
    for row in range(blocks):
        y0 = round(row * height / blocks)
        y1 = round((row + 1) * height / blocks)
        for col in range(blocks):
            x0 = round(col * width / blocks)
            x1 = round((col + 1) * width / blocks)
            block = values[y0:y1, x0:x1]
            if mask is not None:
                block_mask = mask[y0:y1, x0:x1]
                if block_mask.sum() < max(16, block_mask.size * 0.08):
                    continue
                samples.append(float(block[block_mask].mean()))
            else:
                samples.append(float(block.mean()))
    if len(samples) < 2:
        return 0.0
    return float(np.std(samples))


def masked_mean(values: np.ndarray, mask: np.ndarray, fallback: float = 0.0) -> float:
    if mask.sum() <= 0:
        return fallback
    return float(values[mask].mean())


def dilate_mask(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    result = mask.astype(bool)
    for _ in range(max(0, iterations)):
        padded = np.pad(result, 1, mode="constant", constant_values=False)
        result = (
            padded[:-2, :-2] | padded[:-2, 1:-1] | padded[:-2, 2:]
            | padded[1:-1, :-2] | padded[1:-1, 1:-1] | padded[1:-1, 2:]
            | padded[2:, :-2] | padded[2:, 1:-1] | padded[2:, 2:]
        )
    return result


def robust_threshold(values: np.ndarray, mask: np.ndarray, percentile: float, minimum: float) -> float:
    samples = values[mask]
    if samples.size < 64:
        samples = values.reshape(-1)
    return float(max(minimum, np.percentile(samples, percentile)))


def clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def score_candidate(original_path: Path, candidate_path: Path, masks_dir: Path) -> dict:
    """对单张候选图打分。

    全部公式、权重、阈值、指标定义与旧版 `mural-review-tool/server.py`
    逐字相同，没有做任何数学改动。
    """
    original = load_rgb_array(original_path)
    candidate = load_rgb_array(candidate_path)
    if original.shape != candidate.shape:
        raise RuntimeError(f"{candidate_path.name} size does not match original")

    gray_o = luminance(original)
    gray_c = luminance(candidate)
    grad_o = gradient(gray_o)
    grad_c = gradient(gray_c)

    evidence = load_mask(masks_dir / "final_evidence_mask.png")
    line = load_mask(masks_dir / "line_evidence_mask.png")
    pigment = load_mask(masks_dir / "pigment_evidence_mask.png")
    damage = load_mask(masks_dir / "final_damage_mask.png")
    uncertain = load_mask(masks_dir / "final_uncertain_mask.png")
    non_damage = ~damage

    color_delta = np.mean(np.abs(original - candidate), axis=2)
    light_delta = np.abs(gray_o - gray_c)
    grad_delta = np.abs(grad_o - grad_c)
    added_grad = np.clip(grad_c - grad_o, 0.0, 1.0)
    edge_density_delta = abs(masked_mean(grad_c, non_damage) - masked_mean(grad_o, non_damage))
    hue_o, sat_o, val_o = rgb_to_hsv(original)
    hue_c, sat_c, val_c = rgb_to_hsv(candidate)
    hue_delta = hue_distance(hue_o, hue_c)
    pigment_color_weight = pigment & (sat_o >= 0.10)
    abnormal_saturation = np.clip(sat_c - 0.72, 0.0, 1.0)
    evidence_overchange = masked_mean(grad_delta, evidence, 0.0)
    damage_edge_gain = masked_mean(added_grad, damage, float(added_grad.mean()))

    original_edge_threshold = robust_threshold(grad_o, line | evidence, 68.0, 0.08)
    candidate_edge_threshold = robust_threshold(grad_c, line | evidence, 62.0, 0.08)
    original_edges = grad_o >= original_edge_threshold
    candidate_edges = grad_c >= candidate_edge_threshold
    line_core = line & original_edges
    if line_core.sum() < 64:
        line_core = line
    line_near = dilate_mask(line_core, 4)
    evidence_near = dilate_mask(evidence & original_edges, 5)
    candidate_near = dilate_mask(candidate_edges, 2)
    line_recall = masked_mean(candidate_near.astype(float), line_core, 0.0)
    line_strength_gain = masked_mean(np.clip(grad_c - grad_o, 0.0, 1.0), line_core, 0.0)
    line_anchored_gain = masked_mean((candidate_edges & line_near).astype(float), line_near, 0.0)
    unanchored_damage_edges = candidate_edges & damage & ~evidence_near
    unanchored_damage_ratio = masked_mean(unanchored_damage_edges.astype(float), damage, 0.0)
    low_evidence_new_edges = candidate_edges & ~dilate_mask(evidence | line, 4) & ~uncertain
    low_evidence_new_ratio = masked_mean(low_evidence_new_edges.astype(float), damage | (~evidence), 0.0)

    line_fidelity = clamp_score(
        44.0 + line_recall * 48.0 + min(line_strength_gain, 0.12) * 85.0
        + line_anchored_gain * 8.0 - low_evidence_new_ratio * 95.0
    )
    structure = clamp_score(
        100.0 - masked_mean(light_delta, evidence, float(light_delta.mean())) * 70.0
        - masked_mean(grad_delta, evidence, float(grad_delta.mean())) * 55.0
        - max(0.0, 0.78 - line_recall) * 38.0
    )
    hue_penalty = masked_mean(hue_delta, pigment_color_weight, masked_mean(hue_delta, pigment, 0.0))
    cool_warm_flip = masked_mean(
        (np.abs((hue_c > 0.46).astype(float) - (hue_o > 0.46).astype(float))),
        pigment_color_weight, 0.0,
    )
    pigment_direction = clamp_score(100.0 - hue_penalty * 210.0 - cool_warm_flip * 22.0)
    damage_restraint = clamp_score(
        100.0 - unanchored_damage_ratio * 430.0
        - max(0.0, damage_edge_gain - 0.035) * 150.0
        - low_evidence_new_ratio * 95.0
    )
    anomaly = clamp_score(
        100.0 - edge_density_delta * 135.0
        - masked_mean(grad_delta, uncertain, 0.0) * 55.0
        - masked_mean(abnormal_saturation, np.ones_like(damage, dtype=bool), 0.0) * 85.0
        - max(0.0, low_evidence_new_ratio - 0.025) * 120.0
    )

    fill_signal = masked_mean(1.0 - np.clip(light_delta, 0.0, 1.0), damage, 0.55)
    damage_texture = masked_mean(grad_c, damage, 0.0)
    candidate_contrast = float(np.percentile(gray_c, 92.0) - np.percentile(gray_c, 8.0))
    original_contrast = float(np.percentile(gray_o, 92.0) - np.percentile(gray_o, 8.0))
    contrast_gain = max(0.0, min(0.28, candidate_contrast - original_contrast))
    evidence_saturation_gain = max(0.0, masked_mean(sat_c - sat_o, pigment, 0.0))
    display_readability = clamp_score(
        58.0 + min(evidence_saturation_gain, 0.22) * 105.0 + contrast_gain * 75.0
        + line_recall * 15.0 - max(0.0, low_evidence_new_ratio - 0.035) * 70.0
    )
    candidate_edge_density = float(grad_c.mean())
    color_expression = clamp_score(
        20.0 + masked_mean(sat_c, pigment, float(sat_c.mean())) * 180.0
        + masked_mean(sat_c, damage, float(sat_c.mean())) * 25.0
        - max(0.0, masked_mean(val_c, evidence, float(val_c.mean())) - 0.62) * 30.0
    )
    exhibition_clarity = clamp_score(
        30.0 + contrast_gain * 220.0
        + max(0.0, 0.19 - candidate_edge_density) * 240.0
        + min(evidence_saturation_gain, 0.08) * 40.0
        - low_evidence_new_ratio * 45.0
    )
    conservative_vivid_caution = min(
        0.35,
        max(0.0, evidence_saturation_gain - 0.08) * 3.0
        + max(0.0, masked_mean(sat_c, pigment, float(sat_c.mean())) - 0.30) * 2.0,
    )
    completeness = clamp_score(
        44.0 + fill_signal * 26.0 + min(0.20, damage_texture) * 120.0
        + min(evidence_saturation_gain, 0.18) * 45.0 - unanchored_damage_ratio * 60.0
    )
    sat_block_std = block_std(sat_c, blocks=8)
    val_block_std = block_std(val_c, blocks=8)
    grad_block_std = block_std(grad_c, blocks=8)
    damage_evidence_gap = abs(
        masked_mean(sat_c, damage, float(sat_c.mean())) - masked_mean(sat_c, evidence, float(sat_c.mean()))
    )
    style_unity = clamp_score(
        97.0 - sat_block_std * 55.0 - val_block_std * 28.0 - grad_block_std * 45.0
        - max(0.0, damage_evidence_gap - 0.16) * 65.0 - low_evidence_new_ratio * 45.0
    )
    surface_coherence = clamp_score(
        100.0 - grad_block_std * 160.0 - low_evidence_new_ratio * 50.0
        - max(0.0, sat_block_std - 0.045) * 55.0
    )
    composition = clamp_score(
        64.0 + line_recall * 28.0 - masked_mean(grad_delta, evidence, 0.0) * 34.0
        - low_evidence_new_ratio * 60.0
    )
    evidence_compat = clamp_score(
        (line_fidelity * 0.36) + (structure * 0.34) + (pigment_direction * 0.18) + (damage_restraint * 0.12)
    )
    speculation_control = clamp_score((damage_restraint * 0.72) + (anomaly * 0.28))

    conservative_total = clamp_score(
        line_fidelity * 0.39 + structure * 0.12 + pigment_direction * 0.08
        + damage_restraint * 0.25 + anomaly * 0.16 - conservative_vivid_caution
    )
    display_total = clamp_score(
        completeness * 0.04 + display_readability * 0.16 + color_expression * 0.10
        + exhibition_clarity * 0.38 + surface_coherence * 0.12 + style_unity * 0.04
        + composition * 0.06 + evidence_compat * 0.06 + speculation_control * 0.04
    )

    risks = []
    if line_recall < 0.58:
        risks.append("原始线条位置保留不足")
    if damage_restraint < 70:
        risks.append("低证据/损伤区存在较多无锚定新增线条")
    if pigment_direction < 68:
        risks.append("残色证据区色相方向可能跑偏")
    if anomaly < 72:
        risks.append("边缘密度、异常饱和或不确定区变化偏高")
    if not risks:
        risks.append("未发现明显高风险项")

    conservative_reasons = [
        f"原线条位置召回率为 {round(line_recall, 5)}，线条加固强度为 {round(line_strength_gain, 5)}；V2 允许原线条适度加粗，对应线条忠实度 {line_fidelity}。",
        f"损伤区无证据锚定新增线条比例为 {round(unanchored_damage_ratio, 5)}，低证据新增线条比例为 {round(low_evidence_new_ratio, 5)}；对应损伤区克制 {damage_restraint}。",
        f"pigment mask 色相方向差均值为 {round(hue_penalty, 5)}，对应残色方向一致性 {pigment_direction}；鲜艳 caution 为 {round(conservative_vivid_caution, 3)}。",
    ]
    display_reasons = [
        f"展示可读性 {display_readability}，残色区饱和度提升为 {round(evidence_saturation_gain, 5)}，对比度提升为 {round(contrast_gain, 5)}。",
        f"色彩表现 {color_expression}，pigment 区饱和度为 {round(masked_mean(sat_c, pigment, float(sat_c.mean())), 5)}，damage 区饱和度为 {round(masked_mean(sat_c, damage, float(sat_c.mean())), 5)}。",
        f"展陈清晰度 {exhibition_clarity}，由对比度提升 {round(contrast_gain, 5)} 和候选图边缘密度 {round(candidate_edge_density, 5)} 共同决定。",
        f"完整度 {completeness}，damage 区纹理均值为 {round(damage_texture, 5)}，无锚定新增线条比例为 {round(unanchored_damage_ratio, 5)}。",
        f"表面统一 {surface_coherence}，纹理密度块状波动为 {round(grad_block_std, 5)}；构图稳定 {composition}。",
    ]
    explanation = (
        f"V2.1 保守 {conservative_total} / 展示 {display_total}。"
        f"线条 {line_fidelity}，损伤区克制 {damage_restraint}，展示可读性 {display_readability}。"
    )
    _ = color_delta
    return {
        "name": candidate_path.name,
        "url": url_for(candidate_path),
        "previewUrl": f"/api/preview?path={quote(str(candidate_path))}",
        "scores": {
            "lineFidelity": line_fidelity, "structure": structure,
            "pigmentDirection": pigment_direction, "damageRestraint": damage_restraint,
            "anomaly": anomaly, "completeness": completeness, "styleUnity": style_unity,
            "composition": composition, "evidenceCompat": evidence_compat,
            "speculationControl": speculation_control, "displayReadability": display_readability,
            "colorExpression": color_expression, "exhibitionClarity": exhibition_clarity,
            "surfaceCoherence": surface_coherence,
        },
        "rawMetrics": {
            "line_position_recall": round(line_recall, 5),
            "line_strength_gain": round(line_strength_gain, 5),
            "line_anchored_gain": round(line_anchored_gain, 5),
            "line_gradient_delta_mean": round(masked_mean(grad_delta, line, masked_mean(grad_delta, evidence)), 5),
            "evidence_luminance_delta_mean": round(masked_mean(light_delta, evidence, float(light_delta.mean())), 5),
            "evidence_gradient_delta_mean": round(evidence_overchange, 5),
            "pigment_hue_delta_mean": round(hue_penalty, 5),
            "pigment_cool_warm_flip_ratio": round(cool_warm_flip, 5),
            "damage_added_gradient_mean": round(damage_edge_gain, 5),
            "unanchored_damage_edge_ratio": round(unanchored_damage_ratio, 5),
            "low_evidence_new_edge_ratio": round(low_evidence_new_ratio, 5),
            "evidence_saturation_gain": round(evidence_saturation_gain, 5),
            "candidate_pigment_saturation": round(masked_mean(sat_c, pigment, float(sat_c.mean())), 5),
            "candidate_damage_saturation": round(masked_mean(sat_c, damage, float(sat_c.mean())), 5),
            "global_contrast_gain": round(contrast_gain, 5),
            "candidate_edge_density": round(candidate_edge_density, 5),
            "conservative_vivid_caution": round(conservative_vivid_caution, 5),
            "non_damage_edge_density_delta": round(edge_density_delta, 5),
            "abnormal_saturation_mean": round(masked_mean(abnormal_saturation, np.ones_like(damage, dtype=bool), 0.0), 5),
            "candidate_saturation_block_std": round(sat_block_std, 5),
            "candidate_value_block_std": round(val_block_std, 5),
            "candidate_gradient_block_std": round(grad_block_std, 5),
            "damage_evidence_saturation_gap": round(damage_evidence_gap, 5),
        },
        "totals": {"conservative": conservative_total, "display": display_total},
        "risks": risks,
        "reasons": {"conservative": conservative_reasons, "display": display_reasons},
        "explanation": explanation,
    }


def generate_masks(original_path: Path, masks_dir: Path, edge_s: float = 1.0, pigment_s: float = 1.0, missing_s: float = 1.0) -> dict:
    """调 auto_mask.build_masks 生成全部 mask 与指标（算法本体在 auto_mask.py 里，一字未改）。"""
    masks_dir.mkdir(parents=True, exist_ok=True)
    base, arr = AUTO_MASK.load_rgb(original_path, "")
    result = AUTO_MASK.build_masks(arr, edge_s, pigment_s, missing_s)
    evidence = result["evidence"]
    line = result["line_evidence"]
    pigment = result["pigment_evidence"]
    missing = result["missing"]
    damage = result["damage"]
    mud = result["mud_or_occlusion"]
    blank = result["blank_plaster"]
    uncertain = result["uncertain"]

    outputs = {
        "evidence_mask.png": evidence,
        "line_evidence_mask.png": line,
        "pigment_evidence_mask.png": pigment,
        "missing_mask.png": missing,
        "damage_mask.png": damage,
        "mud_or_occlusion_mask.png": mud,
        "blank_plaster_mask.png": blank,
        "uncertain_mask.png": uncertain,
        "final_evidence_mask.png": evidence,
        "final_damage_mask.png": damage,
        "final_uncertain_mask.png": uncertain,
    }
    for name, mask in outputs.items():
        save_mask(mask, masks_dir / name)

    preview = AUTO_MASK.make_preview(base, evidence, damage, uncertain)
    preview.save(masks_dir / "mask_preview.png")
    preview.save(masks_dir / "final_mask_preview.png")

    total = evidence.size
    metrics = {
        "source": str(original_path),
        "width": base.width,
        "height": base.height,
        "evidence_ratio": float(evidence.sum() / total),
        "missing_ratio": float(missing.sum() / total),
        "damage_ratio": float(damage.sum() / total),
        "mud_or_occlusion_ratio": float(mud.sum() / total),
        "blank_plaster_ratio": float(blank.sum() / total),
        "uncertain_ratio": float(uncertain.sum() / total),
        "mask_confidence": AUTO_MASK.confidence_label(
            float(evidence.sum() / total),
            float(missing.sum() / total),
            float(uncertain.sum() / total),
        ),
        "sensitivity": {"edge": edge_s, "pigment": pigment_s, "missing": missing_s},
        "thresholds": {key: float(value) for key, value in result.items() if key.endswith("_threshold")},
    }
    (masks_dir / "mask_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def _rebuild_final_masks(masks_dir: Path, base_image: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """人工修正后重算最终 mask：evidence 与 damage 互斥，uncertain 让位给前两者。"""
    evidence = np.asarray(Image.open(masks_dir / "evidence_mask.png").convert("L")) > 127
    damage = np.asarray(Image.open(masks_dir / "damage_mask.png").convert("L")) > 127
    uncertain = np.asarray(Image.open(masks_dir / "uncertain_mask.png").convert("L")) > 127
    overlap = evidence & damage
    if overlap.any():
        damage[overlap] = False
    uncertain[evidence | damage] = False
    save_mask(evidence, masks_dir / "final_evidence_mask.png")
    save_mask(damage, masks_dir / "final_damage_mask.png")
    save_mask(uncertain, masks_dir / "final_uncertain_mask.png")
    with Image.open(base_image) as base:
        AUTO_MASK.make_preview(base.convert("RGB"), evidence, damage, uncertain).save(
            masks_dir / "final_mask_preview.png"
        )
    return evidence, damage, uncertain


def apply_overrides(project: Path, strokes: list[dict]) -> dict:
    """把人工笔迹画进三张基础 mask，再重算最终 mask（画法语义与旧版一致）。"""
    masks_dir = project / "masks"
    evidence_img = Image.open(masks_dir / "evidence_mask.png").convert("L")
    damage_img = Image.open(masks_dir / "damage_mask.png").convert("L")
    uncertain_img = Image.open(masks_dir / "uncertain_mask.png").convert("L")
    width, height = evidence_img.size
    draw_map = {
        "evidence": ImageDraw.Draw(evidence_img),
        "damage": ImageDraw.Draw(damage_img),
        "uncertain": ImageDraw.Draw(uncertain_img),
    }

    for stroke in strokes:
        mode = stroke.get("mode", "evidence")
        tool = stroke.get("tool", "brush")
        size = max(2, int(stroke.get("size", 24)))
        points = stroke.get("points", [])
        if not points:
            continue
        xy = [(float(p["x"]), float(p["y"])) for p in points if "x" in p and "y" in p]
        if not xy:
            continue
        target_modes = ["evidence", "damage", "uncertain"] if mode == "erase" else [mode]
        other_modes = [] if mode == "erase" else [item for item in ("evidence", "damage", "uncertain") if item != mode]
        fill = 0 if mode == "erase" else 255

        def draw_shape(draw: ImageDraw.ImageDraw, value: int) -> None:
            if tool == "rect" and len(xy) >= 2:
                x0, y0 = xy[0]
                x1, y1 = xy[-1]
                draw.rectangle((x0, y0, x1, y1), fill=value)
            elif len(xy) == 1:
                x, y = xy[0]
                r = size / 2
                draw.ellipse((x - r, y - r, x + r, y + r), fill=value)
            else:
                draw.line(xy, fill=value, width=size, joint="curve")

        for item in target_modes:
            draw_shape(draw_map[item], fill)
        for item in other_modes:
            draw_shape(draw_map[item], 0)

    evidence_img.save(masks_dir / "evidence_mask.png")
    damage_img.save(masks_dir / "damage_mask.png")
    uncertain_img.save(masks_dir / "uncertain_mask.png")

    evidence, damage, uncertain = _rebuild_final_masks(masks_dir, project / "original" / "original.png")
    (masks_dir / "override_strokes.json").write_text(
        json.dumps(strokes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = evidence.size
    return {
        "evidence_ratio": float(evidence.sum() / total),
        "damage_ratio": float(damage.sum() / total),
        "uncertain_ratio": float(uncertain.sum() / total),
        "finalPreviewUrl": url_for(masks_dir / "final_mask_preview.png"),
    }


def reset_overrides(project: Path) -> dict:
    """丢掉人工修正，按项目里记录的灵敏度重新生成自动 mask。"""
    masks_dir = project / "masks"
    metrics_path = masks_dir / "mask_metrics.json"
    edge_s = pigment_s = missing_s = 1.0
    if metrics_path.exists():
        try:
            stored = json.loads(metrics_path.read_text(encoding="utf-8"))
            sens = stored.get("sensitivity", {})
            edge_s = sens.get("edge", 1.0)
            pigment_s = sens.get("pigment", 1.0)
            missing_s = sens.get("missing", 1.0)
        except Exception:
            pass
    metrics = generate_masks(project / "original" / "original.png", masks_dir, edge_s, pigment_s, missing_s)
    return {
        "metrics": metrics,
        "maskPreviewUrl": url_for(masks_dir / "mask_preview.png"),
        "finalPreviewUrl": url_for(masks_dir / "final_mask_preview.png"),
        "evidence_ratio": metrics["evidence_ratio"],
        "damage_ratio": metrics["damage_ratio"],
        "uncertain_ratio": metrics["uncertain_ratio"],
    }


def write_report(project: Path, result: dict, report: Path | None = None) -> Path:
    """生成 Markdown 评分报告（章节顺序、字段与旧版一致）。"""
    report = report or (project / "score_report.md")
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {result['projectName']} 评分报告",
        "",
        f"- 项目 ID：{result['projectId']}",
        f"- 评分版本：{result.get('scoringVersion', SCORING_VERSION)}",
        f"- 评分轮次：{result.get('roundId', '-')}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 保守修复榜",
        "",
    ]
    for index, item in enumerate(result["conservativeRanking"], start=1):
        lines.append(f"{index}. {item['name']} - {item['totals']['conservative']} 分")
    lines += ["", "## 展示复原榜", ""]
    for index, item in enumerate(result["displayRanking"], start=1):
        lines.append(f"{index}. {item['name']} - {item['totals']['display']} 分")
    lines += ["", "## 候选详情", ""]
    for item in result["candidates"]:
        lines.append(f"### {item['name']}")
        lines.append(f"- 保守修复：{item['totals']['conservative']}")
        lines.append(f"- 展示复原：{item['totals']['display']}")
        lines.append(
            "- 分项："
            f"线条 {item['scores']['lineFidelity']}；结构 {item['scores']['structure']}；"
            f"残色方向 {item['scores']['pigmentDirection']}；损伤区克制 {item['scores']['damageRestraint']}；"
            f"展示可读性 {item['scores'].get('displayReadability', '-')}；"
            f"色彩表现 {item['scores'].get('colorExpression', '-')}；"
            f"展陈清晰度 {item['scores'].get('exhibitionClarity', '-')}；"
            f"表面统一 {item['scores'].get('surfaceCoherence', '-')}；"
            f"完整度 {item['scores']['completeness']}；风格统一 {item['scores']['styleUnity']}；"
            f"构图稳定 {item['scores']['composition']}"
        )
        lines.append(f"- 风险：{'；'.join(item['risks'])}")
        lines.append(f"- 说明：{item['explanation']}")
        lines.append("- 保守修复榜理由：")
        for reason in item.get("reasons", {}).get("conservative", []):
            lines.append(f"  - {reason}")
        lines.append("- 展示复原榜理由：")
        for reason in item.get("reasons", {}).get("display", []):
            lines.append(f"  - {reason}")
        lines.append("")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def write_calibration_round(project: Path, result: dict) -> tuple[Path, Path]:
    """每次评分独立归档一份 JSON + MD，便于后续盲选校准。"""
    rounds_dir = project / "calibration_rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    round_id = time.strftime("%Y%m%d_%H%M%S")
    result["roundId"] = round_id
    result["scoringVersion"] = SCORING_VERSION
    result["scoredAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json_path = rounds_dir / f"{round_id}_{SCORING_VERSION}.json"
    report_path = rounds_dir / f"{round_id}_{SCORING_VERSION}.md"
    write_report(project, result, report_path)
    result["roundJsonPath"] = str(json_path)
    result["roundReportPath"] = str(report_path)
    result["roundReportUrl"] = url_for(report_path)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return json_path, report_path


def list_calibration_rounds(project: Path) -> list[dict]:
    rounds_dir = project / "calibration_rounds"
    if not rounds_dir.exists():
        return []
    items = []
    for json_path in sorted(rounds_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            items.append({
                "roundId": data.get("roundId", json_path.stem),
                "scoringVersion": data.get("scoringVersion", SCORING_VERSION),
                "scoredAt": data.get("scoredAt", ""),
                "conservativeWinner": data.get("conservativeRanking", [None])[0].get("name") if data.get("conservativeRanking") else None,
                "displayWinner": data.get("displayRanking", [None])[0].get("name") if data.get("displayRanking") else None,
                "reportUrl": url_for(json_path.with_suffix(".md")),
                "jsonUrl": url_for(json_path),
            })
        except Exception:
            continue
    return items


def create_project_from_uploads(project_name: str, original: Path, candidates: list[Path]) -> dict:
    """从 uploads/ 里已落盘的原图与候选图建一个评分项目。"""
    if not core.is_inside(original, UPLOADS_DIR) or not original.exists():
        raise core.ValidationError(
            "找不到原图，请重新选择并上传。", field="original", detail=f"original={str(original)!r}"
        )
    if not candidates:
        raise core.ValidationError("请至少选择一张候选图。", field="candidates")
    if len(candidates) > MAX_CANDIDATES:
        raise core.ValidationError(
            f"候选图最多 {MAX_CANDIDATES} 张，当前 {len(candidates)} 张。",
            field="candidates",
            detail=f"count={len(candidates)}",
        )
    original = normalize_image_extension(original)
    original_info = image_info(original)
    add_original_history(original, original_info)
    candidate_infos = []
    for candidate in candidates:
        if not core.is_inside(candidate, UPLOADS_DIR) or not candidate.exists():
            raise core.ValidationError(
                "找不到候选图，请重新选择并上传。",
                field="candidates",
                detail=f"candidate={str(candidate)!r}",
            )
        candidate = normalize_image_extension(candidate)
        candidate_infos.append(image_info(candidate))

    project_id = time.strftime("%Y%m%d_%H%M%S_") + str(uuid.uuid4())[:8]
    # 新项目目录此刻还不存在，所以走 _project_path 而不是 project_dir（后者要求已存在）。
    project = _project_path(project_id)
    (project / "original").mkdir(parents=True, exist_ok=True)
    (project / "candidates").mkdir(parents=True, exist_ok=True)
    original_copy = project / "original" / "original.png"
    original_normalized = normalize_image_to_target(original, original_copy)
    copied_candidates = []
    for index, candidate in enumerate(candidates, start=1):
        name = safe_name(Path(candidate_infos[index - 1]["name"]).stem)
        out = project / "candidates" / f"{index:02d}_{name}.png"
        copied_candidates.append(normalize_image_to_target(candidate, out))

    meta = {
        "projectId": project_id,
        "projectName": project_name or "未命名壁画评分项目",
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "targetLongEdge": TARGET_LONG_EDGE,
        "sourceOriginal": original_info,
        "sourceCandidates": candidate_infos,
        "original": original_normalized,
        "candidates": copied_candidates,
    }
    (project / "project.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {**meta, "originalUrl": url_for(original_copy)}


def score_project(project: Path) -> dict:
    """给项目里所有候选打分，生成保守修复榜与展示复原榜（排序规则未改）。"""
    meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
    masks = project / "masks"
    if not (masks / "final_evidence_mask.png").exists():
        generate_masks(project / "original" / "original.png", masks)
    candidates = [
        score_candidate(project / "original" / "original.png", Path(item["path"]), masks)
        for item in meta["candidates"]
    ]
    conservative = sorted(candidates, key=lambda item: item["totals"]["conservative"], reverse=True)
    display = sorted(candidates, key=lambda item: item["totals"]["display"], reverse=True)
    for index, item in enumerate(conservative, start=1):
        item["conservativeRank"] = index
    for index, item in enumerate(display, start=1):
        item["displayRank"] = index
    result = {
        "projectId": meta["projectId"],
        "projectName": meta["projectName"],
        "scoringVersion": SCORING_VERSION,
        "candidates": candidates,
        "conservativeRanking": conservative,
        "displayRanking": display,
    }
    write_calibration_round(project, result)
    report = write_report(project, result)
    result["reportUrl"] = url_for(report)
    result["reportPath"] = str(report)
    (project / "score_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def export_winners(project: Path, result: dict) -> dict:
    """把两张榜的冠军各拷一份到 exports/，便于直接交付。"""
    out_dir = project / "exports"
    conservative_dir = out_dir / "conservative_winner"
    display_dir = out_dir / "display_winner"
    conservative_dir.mkdir(parents=True, exist_ok=True)
    display_dir.mkdir(parents=True, exist_ok=True)

    def copy_winner(item: dict, target_dir: Path, prefix: str) -> dict:
        # 旧版这里是"拼绝对路径 + is_inside"，现在统一走 core.safe_join（S1）。
        source = core.safe_join(ROOT, unquote(item["url"]).lstrip("/"))
        if not source.is_file():
            raise core.NotFoundError(f"冠军图已丢失：{item['name']}", detail=item["url"])
        target = target_dir / f"{prefix}_{safe_name(item['name'])}"
        shutil.copy2(source, target)
        summary = target_dir / "winner_summary.json"
        summary.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "name": item["name"],
            "score": item["totals"]["conservative"] if prefix == "conservative" else item["totals"]["display"],
            "path": str(target),
            "url": url_for(target),
            "summaryPath": str(summary),
        }

    conservative_winner = result["conservativeRanking"][0] if result["conservativeRanking"] else None
    display_winner = result["displayRanking"][0] if result["displayRanking"] else None
    exports = {
        "rootPath": str(out_dir),
        "conservativeDir": str(conservative_dir),
        "displayDir": str(display_dir),
        "conservativeWinner": copy_winner(conservative_winner, conservative_dir, "conservative") if conservative_winner else None,
        "displayWinner": copy_winner(display_winner, display_dir, "display") if display_winner else None,
    }
    (out_dir / "export_summary.json").write_text(
        json.dumps(exports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return exports


def export_project_zip(project: Path) -> dict:
    out_dir = project / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{project.name}_archive.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in project.rglob("*"):
            if path.is_file() and not path.name.endswith(".zip"):
                archive.write(path, path.relative_to(project))
    return {"path": str(zip_path), "url": url_for(zip_path), "size": zip_path.stat().st_size}


# ==========================================================================
# 参数校验（SERIES-SPEC §7 / S4、S5）
# ==========================================================================


def content_length(handler, *, limit: int, label: str) -> int:
    """读取并校验 Content-Length；超限抛 413，并给出可直接显示的中文提示。"""
    raw = handler.headers.get("Content-Length")
    if raw is None:
        raise core.ValidationError(f"请求缺少 Content-Length，无法接收{label}。")
    try:
        length = int(raw)
    except ValueError:
        raise core.ValidationError("Content-Length 不合法。", detail=f"value={raw!r}")
    if length <= 0:
        raise core.ValidationError(f"{label}内容为空。")
    if length > limit:
        raise core.PayloadTooLargeError(
            f"{label}过大，单次上限 {limit // (1024 * 1024)} MB。",
            detail=f"content-length={length} limit={limit}",
        )
    return length


def read_body_to_file(rfile, dest: Path, length: int) -> int:
    """把请求体按块写入文件（S6：不整段读进内存）。"""
    written = 0
    with dest.open("wb") as handle:
        while written < length:
            chunk = rfile.read(min(1024 * 1024, length - written))
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
    return written


def _clamp_float(payload: dict, key: str, default: float, low: float, high: float, label: str) -> float:
    """数值参数上限校验（S5）：越界返回 400 + 中文 message。"""
    raw = payload.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise core.ValidationError(f"{label}必须是数字。", field=key, detail=f"value={raw!r}")
    if value != value:  # NaN
        raise core.ValidationError(f"{label}必须是数字。", field=key, detail="value=nan")
    if value < low or value > high:
        raise core.ValidationError(
            f"{label}必须在 {low} 到 {high} 之间。", field=key, detail=f"value={value}"
        )
    return value


def validate_strokes(raw) -> list[dict]:
    """校验人工修正笔迹（S5）。

    只做"防病态请求"的边界检查与字段清洗，不改任何画法语义：
    通过校验后交给 apply_overrides 的字典结构与旧版完全一致。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise core.ValidationError("strokes 必须是数组。", field="strokes")
    if len(raw) > MAX_STROKES:
        raise core.ValidationError(
            f"修正笔迹最多 {MAX_STROKES} 条，当前 {len(raw)} 条。",
            field="strokes",
            detail=f"count={len(raw)}",
        )
    total_points = 0
    cleaned: list[dict] = []
    for index, stroke in enumerate(raw):
        if not isinstance(stroke, dict):
            raise core.ValidationError("每条笔迹必须是对象。", field="strokes", detail=f"index={index}")
        mode = stroke.get("mode", "evidence")
        if mode not in ("evidence", "damage", "uncertain", "erase"):
            raise core.ValidationError(
                "笔迹模式只能是 evidence / damage / uncertain / erase。",
                field="strokes", detail=f"index={index} mode={mode!r}",
            )
        tool = stroke.get("tool", "brush")
        if tool not in ("brush", "rect"):
            raise core.ValidationError(
                "画笔类型只能是 brush / rect。", field="strokes", detail=f"index={index} tool={tool!r}"
            )
        try:
            size = int(stroke.get("size", 24))
        except (TypeError, ValueError):
            raise core.ValidationError("笔刷尺寸必须是整数。", field="strokes", detail=f"index={index}")
        size = max(2, min(MAX_BRUSH_SIZE, size))
        points = stroke.get("points", [])
        if not isinstance(points, list):
            raise core.ValidationError("笔迹点必须是数组。", field="strokes", detail=f"index={index}")
        total_points += len(points)
        if total_points > MAX_STROKE_POINTS:
            raise core.ValidationError(
                f"修正笔迹总点数最多 {MAX_STROKE_POINTS}，已超出。",
                field="strokes", detail=f"points={total_points}",
            )
        cleaned.append({**stroke, "mode": mode, "tool": tool, "size": size, "points": points})
    return cleaned


# ==========================================================================
# HTTP 层
# ==========================================================================


class Handler(BaseHTTPRequestHandler):
    server_version = f"BaizeReview/{APP_VERSION}"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ----- 基础响应（全部走 toolkit_core） -----

    def send_ok(self, payload: dict | None = None) -> None:
        core.api_ok(self, payload)

    def serve_file(self, path: Path, ctype: str | None = None) -> None:
        core.stream_file(self, path, ctype)

    # ----- 路由 -----

    def do_OPTIONS(self) -> None:  # noqa: N802
        core.handle_options(self)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = core.urlparse_path(self.path)
        try:
            if path == "/api/health":
                self.send_ok(core.health_payload(
                    APP_SLUG, APP_VERSION, self.server.server_address[1],
                    name=APP_NAME, nameEn=APP_NAME_EN,
                    scoringVersion=SCORING_VERSION,
                    autoMaskLoaded=AUTO_MASK is not None,
                    runsMaxAgeDays=RUNS_MAX_AGE_DAYS,
                    targetLongEdge=TARGET_LONG_EDGE,
                    maxCandidates=MAX_CANDIDATES,
                    maxUploadBytes=MAX_UPLOAD_BYTES,
                ))
                return

            if path == "/api/history":
                data = read_history()
                originals = [
                    item for item in data.get("originals", [])
                    if item.get("path") and Path(item["path"]).exists()
                ]
                if len(originals) != len(data.get("originals", [])):
                    data["originals"] = originals
                    write_history(data)
                self.send_ok(data)
                return

            if path == "/api/preview":
                qs = parse_qs(parsed.query)
                target = resolve_tool_path(qs.get("path", [""])[0])
                if not target.is_file():
                    raise core.NotFoundError(f"找不到图片：{target.name}", detail=f"path={str(target)!r}")
                self.serve_file(ensure_preview(target))
                return

            if path in ("/", "/index.html"):
                target = core.safe_join(WEB_ROOT, "index.html", must_exist=True)
                self.serve_file(target)
                return

            if path.startswith("/assets/"):
                rest = path[len("/assets/"):]
                target = core.safe_join(ASSET_ROOT, rest)
                self.serve_file(target)
                return

            if path.startswith("/runs/"):
                rest = path[len("/runs/"):]
                target = core.safe_join(RUNS_DIR, rest)
                self.serve_file(target)
                return

            # 静态文件：web/ 之内，越界由 safe_join 抛 PathEscapeError（S1）
            rel = path.lstrip("/") or "index.html"
            if core.serve_static(self, WEB_ROOT, rel):
                return

            raise core.NotFoundError(f"找不到页面：{path}", detail=path)
        except Exception as exc:
            core.api_exception(self, exc)

    def do_POST(self) -> None:  # noqa: N802
        path = core.urlparse_path(self.path)
        try:
            if path == "/api/upload":
                self.handle_upload()
                return
            if path == "/api/pick-file":
                self.handle_pick_file()
                return
            if path == "/api/pick-folder":
                self.handle_pick_folder()
                return

            if path == "/api/save-override":
                # 笔迹 JSON 天然比普通表单大，这里单独放宽到 8 MB（见 CHANGELOG「已知取舍」）。
                payload = core.read_json(self, limit=OVERRIDE_JSON_LIMIT)
                project = project_dir(payload.get("projectId", ""))
                if not (project / "masks" / "evidence_mask.png").is_file():
                    # 旧版这里会直接 FileNotFoundError → 500；换成能直接显示的中文提示。
                    raise core.ValidationError(
                        "这个项目还没有 mask，请先点「生成 mask」再保存人工修正。",
                        field="projectId",
                    )
                result = apply_overrides(project, validate_strokes(payload.get("strokes", [])))
                self.send_ok(result)
                return

            # 其余 JSON 接口统一走底座的 1 MB 上限（S3）
            payload = core.read_json(self)

            if path == "/api/create-project":
                original = Path(str(payload.get("original", ""))).resolve()
                candidates = [Path(str(item)).resolve() for item in payload.get("candidates", [])]
                self.send_ok(create_project_from_uploads(payload.get("projectName", ""), original, candidates))
                return

            if path == "/api/generate-mask":
                project = project_dir(payload.get("projectId", ""))
                original = project / "original" / "original.png"
                edge_s = _clamp_float(payload, "edgeSensitivity", 1.0, *SENSITIVITY_RANGE, "线条边缘灵敏度 edge")
                pigment_s = _clamp_float(payload, "pigmentSensitivity", 1.0, *SENSITIVITY_RANGE, "残色灵敏度 pigment")
                missing_s = _clamp_float(payload, "missingSensitivity", 1.0, *SENSITIVITY_RANGE, "缺失灵敏度 missing")
                metrics = generate_masks(original, project / "masks", edge_s, pigment_s, missing_s)
                self.send_ok({
                    "metrics": metrics,
                    "maskPreviewUrl": url_for(project / "masks" / "mask_preview.png"),
                    "finalPreviewUrl": url_for(project / "masks" / "final_mask_preview.png"),
                    "maskUrls": {
                        name: url_for(project / "masks" / name)
                        for name in [
                            "evidence_mask.png", "line_evidence_mask.png", "pigment_evidence_mask.png",
                            "damage_mask.png", "missing_mask.png", "mud_or_occlusion_mask.png",
                            "blank_plaster_mask.png", "uncertain_mask.png",
                        ]
                    },
                })
                return

            if path == "/api/reset-override":
                project = project_dir(payload.get("projectId", ""))
                self.send_ok(reset_overrides(project))
                return

            if path == "/api/score":
                project = project_dir(payload.get("projectId", ""))
                result = score_project(project)
                result["calibrationRounds"] = list_calibration_rounds(project)
                self.send_ok(result)
                return

            if path == "/api/calibration-rounds":
                project = project_dir(payload.get("projectId", ""))
                self.send_ok({"rounds": list_calibration_rounds(project)})
                return

            if path == "/api/export-zip":
                project = project_dir(payload.get("projectId", ""))
                self.send_ok(export_project_zip(project))
                return

            if path == "/api/auto-review":
                original = Path(str(payload.get("original", ""))).resolve()
                candidates = [Path(str(item)).resolve() for item in payload.get("candidates", [])]
                project_meta = create_project_from_uploads(payload.get("projectName", ""), original, candidates)
                project = project_dir(project_meta["projectId"])
                edge_s = _clamp_float(payload, "edgeSensitivity", 1.0, *SENSITIVITY_RANGE, "线条边缘灵敏度 edge")
                pigment_s = _clamp_float(payload, "pigmentSensitivity", 1.0, *SENSITIVITY_RANGE, "残色灵敏度 pigment")
                missing_s = _clamp_float(payload, "missingSensitivity", 1.0, *SENSITIVITY_RANGE, "缺失灵敏度 missing")
                mask_payload = generate_masks(project / "original" / "original.png", project / "masks", edge_s, pigment_s, missing_s)
                result = score_project(project)
                exports = export_winners(project, result)
                result["calibrationRounds"] = list_calibration_rounds(project)
                self.send_ok({
                    "project": project_meta,
                    "metrics": mask_payload,
                    "score": result,
                    "exports": exports,
                })
                return

            raise core.NotFoundError(f"找不到接口：{path}", detail=path)
        except Exception as exc:
            core.api_exception(self, exc)

    # ----- 业务处理 -----

    def handle_upload(self) -> None:
        """单文件上传（S4：大小上限 + 同名不覆盖）。"""
        filename = core.safe_filename(unquote(self.headers.get("X-Filename", "upload.png")))
        role = core.safe_filename(self.headers.get("X-Role", "candidate"))
        length = content_length(self, limit=MAX_UPLOAD_BYTES, label="上传文件")

        upload_id = str(uuid.uuid4())
        directory = UPLOADS_DIR / upload_id
        directory.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix or ".png"
        # 同名文件不覆盖：重名自动加序号（SERIES-SPEC §7 / S4）
        out = core.unique_path(directory, f"{role}{suffix}")
        read_body_to_file(self.rfile, out, length)
        out = normalize_image_extension(out)
        info = image_info(out)
        info["uploadId"] = upload_id
        info["receivedBytes"] = length
        self.send_ok(info)

    def handle_pick_file(self) -> None:
        payload = core.read_json(self)
        kind = payload.get("kind", "image")
        multi = bool(payload.get("multi", False))
        if kind == "image":
            filters = [("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff"), ("All files", "*.*")]
        else:
            filters = [("All files", "*.*")]
        if multi:
            picked = choose_files_windows("选择图片（可多选）", filters) if sys.platform.startswith("win") else []
            self.send_ok({"paths": picked})
        else:
            picked = choose_file_windows("选择图片", filters) if sys.platform.startswith("win") else ""
            self.send_ok({"path": picked})

    def handle_pick_folder(self) -> None:
        payload = core.read_json(self)
        title = str(payload.get("title", "选择文件夹"))
        picked = ""
        if sys.platform.startswith("win"):
            command = (
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$f=New-Object System.Windows.Forms.Form;"
                "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.WindowState='Minimized';$f.Show();"
                "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                f"$d.Title={powershell_quote(title)};"
                "$d.Filter='Folders|*.folder';$d.CheckFileExists=$false;"
                "$d.CheckPathExists=$true;$d.ValidateNames=$false;$d.FileName='选择此文件夹';"
                "if($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK)"
                "{if([System.IO.Directory]::Exists($d.FileName)){Write-Output $d.FileName}"
                "else{Write-Output ([System.IO.Path]::GetDirectoryName($d.FileName))}};"
                "$f.Close();"
            )
            picked = clean_dialog_output(run_powershell_dialog(command))
        self.send_ok({"path": picked})


def main() -> int:
    host = "127.0.0.1"  # S7：只监听本机
    port = int(os.environ.get("BAIZE_PORT", str(DEFAULT_PORT)))
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"端口不合法：{sys.argv[1]}")
            return 2
    httpd = ThreadingHTTPServer((host, port), Handler)
    core.print_banner(APP_NAME, APP_NAME_EN, APP_VERSION, port)
    print(f"  打分算法版本：{SCORING_VERSION}")
    print(f"  运行目录：{RUNS_DIR}")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
