#!/usr/bin/env python
"""Generate lightweight mural evidence and missing-area masks.

This script intentionally avoids heavy segmentation models. It uses only
Pillow and NumPy so the first pass is easy to run, inspect, and tune.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate evidence/missing masks for mural restoration scoring."
    )
    parser.add_argument("--ref", required=True, help="Path to the original mural image.")
    parser.add_argument("--out", required=True, help="Output directory for masks.")
    parser.add_argument(
        "--crop",
        default="",
        help=(
            "Optional crop before mask generation. Use left-half, right-half, "
            "or x,y,w,h in pixels."
        ),
    )
    parser.add_argument(
        "--edge-sensitivity",
        type=float,
        default=1.0,
        help="Higher values keep more faint line evidence. Default: 1.0",
    )
    parser.add_argument(
        "--pigment-sensitivity",
        type=float,
        default=1.0,
        help="Higher values keep more faint pigment evidence. Default: 1.0",
    )
    parser.add_argument(
        "--missing-sensitivity",
        type=float,
        default=1.0,
        help="Higher values mark more pale/low-evidence areas as missing. Default: 1.0",
    )
    return parser.parse_args()


def apply_crop(image: Image.Image, crop: str) -> Image.Image:
    crop = crop.strip().lower()
    if not crop:
        return image
    width, height = image.size
    if crop == "left-half":
        return image.crop((0, 0, width // 2, height))
    if crop == "right-half":
        return image.crop((width // 2, 0, width, height))
    parts = [part.strip() for part in crop.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop must be left-half, right-half, or x,y,w,h")
    x, y, w, h = [int(part) for part in parts]
    if w <= 0 or h <= 0:
        raise ValueError("--crop width and height must be positive")
    return image.crop((x, y, x + w, y + h))


def load_rgb(path: Path, crop: str) -> tuple[Image.Image, np.ndarray]:
    image = Image.open(path).convert("RGB")
    image = apply_crop(image, crop)
    arr = np.asarray(image).astype(np.float32) / 255.0
    return image, arr


def save_mask(mask: np.ndarray, path: Path) -> None:
    Image.fromarray((mask.astype(np.uint8) * 255), mode="L").save(path)


def percentile_stretch(values: np.ndarray, low: float = 1.0, high: float = 99.0) -> np.ndarray:
    lo, hi = np.percentile(values, [low, high])
    if hi <= lo:
        return np.zeros_like(values)
    return np.clip((values - lo) / (hi - lo), 0.0, 1.0)


def otsu_threshold(values: np.ndarray) -> float:
    values = np.clip(values, 0.0, 1.0)
    hist, bin_edges = np.histogram(values.ravel(), bins=256, range=(0.0, 1.0))
    total = values.size
    if total == 0 or hist.sum() == 0:
        return 0.5

    centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    mean_bg = np.cumsum(hist * centers) / np.maximum(weight_bg, 1)
    mean_fg = (np.cumsum((hist * centers)[::-1]) / np.maximum(np.cumsum(hist[::-1]), 1))[::-1]
    variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    return float(centers[int(np.nanargmax(variance))])


def sobel_magnitude(gray: np.ndarray) -> np.ndarray:
    padded = np.pad(gray, 1, mode="edge")
    gx = (
        -padded[:-2, :-2]
        + padded[:-2, 2:]
        - 2 * padded[1:-1, :-2]
        + 2 * padded[1:-1, 2:]
        - padded[2:, :-2]
        + padded[2:, 2:]
    )
    gy = (
        -padded[:-2, :-2]
        - 2 * padded[:-2, 1:-1]
        - padded[:-2, 2:]
        + padded[2:, :-2]
        + 2 * padded[2:, 1:-1]
        + padded[2:, 2:]
    )
    return percentile_stretch(np.sqrt(gx * gx + gy * gy))


def max_filter(mask: np.ndarray, size: int) -> np.ndarray:
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    return np.asarray(image.filter(ImageFilter.MaxFilter(size=size))) > 0


def min_filter(mask: np.ndarray, size: int) -> np.ndarray:
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    return np.asarray(image.filter(ImageFilter.MinFilter(size=size))) > 0


def open_mask(mask: np.ndarray, size: int = 3) -> np.ndarray:
    return max_filter(min_filter(mask, size), size)


def close_mask(mask: np.ndarray, size: int = 5) -> np.ndarray:
    return min_filter(max_filter(mask, size), size)


def coarsen_mask(mask: np.ndarray, factor: int = 4, threshold: float = 0.28) -> np.ndarray:
    height, width = mask.shape
    small_size = (max(1, width // factor), max(1, height // factor))
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    small = image.resize(small_size, Image.Resampling.BOX)
    coarse = np.asarray(small).astype(np.float32) / 255.0 >= threshold
    large = Image.fromarray((coarse.astype(np.uint8) * 255), mode="L").resize(
        (width, height), Image.Resampling.NEAREST
    )
    return np.asarray(large) > 0


def block_density(mask: np.ndarray, factor: int = 32) -> np.ndarray:
    height, width = mask.shape
    small_size = (max(1, width // factor), max(1, height // factor))
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    small = image.resize(small_size, Image.Resampling.BOX)
    large = small.resize((width, height), Image.Resampling.BILINEAR)
    return np.asarray(large).astype(np.float32) / 255.0


def build_masks(arr: np.ndarray, edge_s: float, pigment_s: float, missing_s: float) -> dict[str, np.ndarray | float]:
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    maxc = np.max(arr, axis=2)
    minc = np.min(arr, axis=2)
    saturation = (maxc - minc) / np.maximum(maxc, 1e-4)
    colorfulness = percentile_stretch(maxc - minc)

    blur = np.asarray(
        Image.fromarray((luminance * 255).astype(np.uint8), mode="L").filter(
            ImageFilter.GaussianBlur(radius=2.0)
        )
    ).astype(np.float32) / 255.0
    local_contrast = percentile_stretch(np.abs(luminance - blur))
    gradient = sobel_magnitude(blur)
    line_signal = np.maximum(gradient, local_contrast)

    edge_threshold = max(0.04, otsu_threshold(line_signal) * (1.15 / max(edge_s, 0.2)))
    pigment_threshold = max(
        0.025,
        min(np.percentile(saturation, 70), 0.18) * (0.95 / max(pigment_s, 0.2)),
    )

    edge_mask = line_signal >= edge_threshold
    pigment_mask = (saturation >= pigment_threshold) & (colorfulness >= 0.08) & (luminance < 0.92)

    brownness = np.clip((r - b) + 0.35 * (g - b), 0.0, 1.0)
    mud_candidate = (
        (brownness >= max(0.035, np.percentile(brownness, 62)))
        & (luminance >= 0.22)
        & (luminance <= 0.72)
        & (saturation <= max(0.35, np.percentile(saturation, 88)))
        & (line_signal <= max(0.20, np.percentile(line_signal, 72)))
    )
    mud_mask = close_mask(open_mask(mud_candidate, 5), 11)

    # Broad pigment stains are useful context, but they should not turn the
    # whole wall into protected evidence. Keep pigment in the main evidence mask
    # only when it is strong or close to detected line structure.
    strong_pigment = pigment_mask & (
        (colorfulness >= np.percentile(colorfulness, 82))
        | (saturation >= np.percentile(saturation, 82))
        | max_filter(edge_mask, 7)
    )
    signal_seed = edge_mask | strong_pigment
    signal_density = block_density(signal_seed, factor=48)
    blank_candidate = (
        (signal_density <= 0.22)
        & (luminance >= max(0.50, np.percentile(luminance, 45)))
        & (saturation <= max(0.18, np.percentile(saturation, 62)))
        & (colorfulness <= max(0.22, np.percentile(colorfulness, 72)))
        & ~max_filter(signal_seed, 7)
    )
    blank_plaster = coarsen_mask(blank_candidate, factor=24, threshold=0.36)
    blank_plaster = close_mask(open_mask(blank_plaster, 9), 17)
    damage_seed = mud_mask | blank_plaster
    evidence = (max_filter(edge_mask, 3) | open_mask(strong_pigment, 3)) & ~max_filter(damage_seed, 3)

    pale_threshold = min(0.92, max(0.62, np.percentile(luminance, 68) * (1.0 / max(missing_s, 0.2))))
    low_saturation_threshold = max(0.08, np.percentile(saturation, 45) * (1.25 / max(missing_s, 0.2)))
    low_signal_threshold = max(0.06, np.percentile(line_signal, 42) * (1.35 / max(missing_s, 0.2)))

    missing = (
        (luminance >= pale_threshold)
        & (saturation <= low_saturation_threshold)
        & (line_signal <= low_signal_threshold)
    )
    # Missing areas should represent coherent loss/low-evidence patches, not
    # salt-and-pepper wall texture. Coarsen first so tiny speckles disappear
    # while broader pale losses remain.
    missing = coarsen_mask(missing, factor=4, threshold=0.28)
    missing = close_mask(missing, 9)
    missing = open_mask(missing, 9)
    missing = missing & ~max_filter(evidence, 9)
    damage = mud_mask | blank_plaster | missing

    near_line_threshold = np.abs(line_signal - edge_threshold) <= 0.045
    near_missing_threshold = (
        (np.abs(luminance - pale_threshold) <= 0.045)
        & (saturation <= low_saturation_threshold * 1.35)
    )
    weak_pigment = pigment_mask & ~strong_pigment & ~max_filter(edge_mask, 5)
    uncertain = (near_line_threshold | near_missing_threshold | weak_pigment) & ~(evidence | damage)
    uncertain = open_mask(uncertain, 5)

    return {
        "evidence": evidence,
        "line_evidence": edge_mask,
        "pigment_evidence": pigment_mask,
        "missing": missing,
        "damage": damage,
        "mud_or_occlusion": mud_mask,
        "blank_plaster": blank_plaster,
        "uncertain": uncertain,
        "edge_threshold": edge_threshold,
        "pigment_threshold": pigment_threshold,
        "pale_threshold": pale_threshold,
        "low_saturation_threshold": low_saturation_threshold,
        "low_signal_threshold": low_signal_threshold,
    }


def make_preview(base: Image.Image, evidence: np.ndarray, damage: np.ndarray, uncertain: np.ndarray) -> Image.Image:
    arr = np.asarray(base).astype(np.float32)
    overlay = arr.copy()
    overlay[evidence] = overlay[evidence] * 0.45 + np.array([0, 220, 120], dtype=np.float32) * 0.55
    overlay[damage] = overlay[damage] * 0.45 + np.array([255, 80, 40], dtype=np.float32) * 0.55
    overlay[uncertain] = overlay[uncertain] * 0.82 + np.array([80, 120, 255], dtype=np.float32) * 0.18
    return Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8), mode="RGB")


def confidence_label(evidence_ratio: float, missing_ratio: float, uncertain_ratio: float) -> str:
    if evidence_ratio < 0.015 or evidence_ratio > 0.75:
        return "low"
    if missing_ratio > 0.85 or uncertain_ratio > 0.85:
        return "low"
    if evidence_ratio < 0.04 or missing_ratio > 0.65 or uncertain_ratio > 0.65:
        return "medium"
    return "high"


def main() -> None:
    args = parse_args()
    ref_path = Path(args.ref)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    base, arr = load_rgb(ref_path, args.crop)
    result = build_masks(arr, args.edge_sensitivity, args.pigment_sensitivity, args.missing_sensitivity)
    evidence = result["evidence"]
    line_evidence = result["line_evidence"]
    pigment_evidence = result["pigment_evidence"]
    missing = result["missing"]
    damage = result["damage"]
    mud_or_occlusion = result["mud_or_occlusion"]
    blank_plaster = result["blank_plaster"]
    uncertain = result["uncertain"]

    evidence_path = out_dir / "evidence_mask.png"
    line_evidence_path = out_dir / "line_evidence_mask.png"
    pigment_evidence_path = out_dir / "pigment_evidence_mask.png"
    missing_path = out_dir / "missing_mask.png"
    damage_path = out_dir / "damage_mask.png"
    mud_or_occlusion_path = out_dir / "mud_or_occlusion_mask.png"
    blank_plaster_path = out_dir / "blank_plaster_mask.png"
    uncertain_path = out_dir / "uncertain_mask.png"
    preview_path = out_dir / "mask_preview.png"
    metrics_path = out_dir / "mask_metrics.json"

    save_mask(evidence, evidence_path)
    save_mask(line_evidence, line_evidence_path)
    save_mask(pigment_evidence, pigment_evidence_path)
    save_mask(missing, missing_path)
    save_mask(damage, damage_path)
    save_mask(mud_or_occlusion, mud_or_occlusion_path)
    save_mask(blank_plaster, blank_plaster_path)
    save_mask(uncertain, uncertain_path)
    make_preview(base, evidence, damage, uncertain).save(preview_path)

    total = evidence.size
    evidence_ratio = float(evidence.sum() / total)
    missing_ratio = float(missing.sum() / total)
    damage_ratio = float(damage.sum() / total)
    mud_or_occlusion_ratio = float(mud_or_occlusion.sum() / total)
    blank_plaster_ratio = float(blank_plaster.sum() / total)
    uncertain_ratio = float(uncertain.sum() / total)
    metrics = {
        "source": str(ref_path),
        "crop": args.crop or None,
        "width": base.width,
        "height": base.height,
        "evidence_ratio": evidence_ratio,
        "missing_ratio": missing_ratio,
        "damage_ratio": damage_ratio,
        "mud_or_occlusion_ratio": mud_or_occlusion_ratio,
        "blank_plaster_ratio": blank_plaster_ratio,
        "uncertain_ratio": uncertain_ratio,
        "mask_confidence": confidence_label(evidence_ratio, missing_ratio, uncertain_ratio),
        "thresholds": {
            key: float(value)
            for key, value in result.items()
            if key.endswith("_threshold")
        },
        "legend": {
            "mask_preview_green": "evidence/protected original traces",
            "mask_preview_orange": "missing/damage/low-evidence areas",
            "mask_preview_blue": "uncertain background/context",
        },
    }
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote: {evidence_path}")
    print(f"wrote: {line_evidence_path}")
    print(f"wrote: {pigment_evidence_path}")
    print(f"wrote: {missing_path}")
    print(f"wrote: {damage_path}")
    print(f"wrote: {mud_or_occlusion_path}")
    print(f"wrote: {blank_plaster_path}")
    print(f"wrote: {uncertain_path}")
    print(f"wrote: {preview_path}")
    print(f"wrote: {metrics_path}")
    print(f"mask_confidence: {metrics['mask_confidence']}")
    print(
        "ratios: "
        f"evidence={evidence_ratio:.3f}, "
        f"missing={missing_ratio:.3f}, "
        f"damage={damage_ratio:.3f}, "
        f"uncertain={uncertain_ratio:.3f}"
    )


if __name__ == "__main__":
    main()
