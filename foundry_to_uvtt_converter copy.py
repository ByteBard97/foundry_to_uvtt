import json
import argparse
import pathlib
import math
from typing import Tuple, Dict, Any, List

# ------------------------------------------------------------
# Helper utilities
# ------------------------------------------------------------

def _get_pixels_per_grid(scene: Dict[str, Any]) -> int:
    """Return pixels‑per‑grid for the scene, defaulting to 140."""
    grid = scene.get("grid", 140)
    if isinstance(grid, dict):
        return int(grid.get("size", 140) or 140)
    return int(grid)


def _get_grid_shift(scene: Dict[str, Any]) -> Tuple[float, float]:
    """Extract Foundry's grid.shiftX / shiftY in *pixels*."""
    grid = scene.get("grid", {})
    if isinstance(grid, dict):
        return float(grid.get("shiftX", 0) or 0), float(grid.get("shiftY", 0) or 0)
    # Older exports sometimes store shift at the top level
    return float(scene.get("shiftX", 0) or 0), float(scene.get("shiftY", 0) or 0)


def _get_padding_offset(scene: Dict[str, Any]) -> Tuple[float, float]:
    """Canvas padding is stored as a *fraction* of the scene size."""
    padding = float(scene.get("padding", 0) or 0)
    if padding == 0:
        return 0.0, 0.0
    return padding * scene.get("width", 0), padding * scene.get("height", 0)


def _get_background_shift(scene: Dict[str, Any]) -> Tuple[float, float]:
    """Return the background image's centre offset from the scene centre (px)."""
    bg = scene.get("background", {})
    sw = scene.get("width", 0)
    sh = scene.get("height", 0)
    if not bg:
        return 0.0, 0.0
    cx = float(bg.get("x", sw / 2))
    cy = float(bg.get("y", sh / 2))
    return cx - sw / 2, cy - sh / 2

# ------------------------------------------------------------
# Core converter
# ------------------------------------------------------------

def convert_foundry_to_uvtt(scene: Dict[str, Any], *, expand_map: bool = True) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Convert one Foundry scene → Universal VTT dict (v1.0).

    Parameters
    ----------
    scene : dict
        Parsed JSON line containing the scene.
    expand_map : bool, default True
        Grow ``resolution.map_size`` so every vertex lies inside the rectangle
        Roll20 expects.  Prevents NaNs when walls overrun the nominal canvas.
    """
    sw = float(scene.get("width", 0))
    sh = float(scene.get("height", 0))
    if sw <= 0 or sh <= 0:
        raise ValueError("Scene is missing valid 'width' / 'height' fields")

    ppg = _get_pixels_per_grid(scene)
    shift_x, shift_y = _get_grid_shift(scene)
    pad_x, pad_y = _get_padding_offset(scene)
    bg_x, bg_y = _get_background_shift(scene)

    # Total translation in *pixels*
    off_x = pad_x + bg_x + shift_x
    off_y = pad_y + bg_y + shift_y

    los: List[List[Dict[str, float]]] = []
    portals: List[Dict[str, Any]] = []
    max_gx = max_gy = 0.0

    for seg in scene.get("walls", []):
        c = seg.get("c")
        if not (isinstance(c, list) and len(c) == 4):
            continue
        x1, y1, x2, y2 = map(float, c)

        # --- translate & scale to grid units ---
        gx1 = (x1 - off_x) / ppg
        gy1 = (y1 - off_y) / ppg
        gx2 = (x2 - off_x) / ppg
        gy2 = (y2 - off_y) / ppg

        if not all(map(math.isfinite, (gx1, gy1, gx2, gy2))):
            # Skip bad vertices early – they would break Roll20.
            continue

        max_gx = max(max_gx, gx1, gx2)
        max_gy = max(max_gy, gy1, gy2)

        if seg.get("door", 0) == 1:
            mid_x, mid_y = (gx1 + gx2) / 2, (gy1 + gy2) / 2
            portals.append({
                "position": {"x": mid_x, "y": mid_y},
                "bounds": [{"x": gx1, "y": gy1}, {"x": gx2, "y": gy2}],
                "closed": seg.get("ds", 0) == 0,
                "freestanding": False
            })
        else:
            los.append([{"x": gx1, "y": gy1}, {"x": gx2, "y": gy2}])

    # --- final map size (grid units) ---
    map_x = sw / ppg
    map_y = sh / ppg
    if expand_map:
        map_x = max(map_x, math.ceil(max_gx + 1e-6))
        map_y = max(map_y, math.ceil(max_gy + 1e-6))

    uvtt = {
        "format": 1.0,
        "resolution": {
            "map_origin": {"x": 0, "y": 0},
            "map_size": {"x": map_x, "y": map_y},
            "pixels_per_grid": ppg
        },
        "image": scene.get("background", {}).get("src", ""),
        "line_of_sight": los,
        "portals": portals
    }

    dbg = {
        "scene_px": f"{sw}×{sh}",
        "ppg": ppg,
        "grid_shift_px": (shift_x, shift_y),
        "padding_px": (pad_x, pad_y),
        "background_shift_px": (bg_x, bg_y),
        "total_offset_px": (off_x, off_y),
        "max_vertex_grid": (max_gx, max_gy),
        "final_map_size_grid": (map_x, map_y),
        "walls": len(los),
        "portals": len(portals)
    }
    return uvtt, dbg

# ------------------------------------------------------------
# File I/O helpers
# ------------------------------------------------------------

def _load_first_scene(path: pathlib.Path) -> Dict[str, Any]:
    """Return the first JSON object in *path* that contains a 'walls' array."""
    with path.open(encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "walls" in obj:
                return obj
    raise ValueError("No scene with a 'walls' array found in file.")

# ------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Convert Foundry VTT ➜ Universal VTT (deterministic offsets)")
    ap.add_argument("input", type=pathlib.Path, help="Foundry .db/.json file")
    ap.add_argument("-o", "--output", type=pathlib.Path, help="Target .uvtt path (defaults to input name)")
    ap.add_argument("--no-expand", action="store_true", help="Disable map‑size expansion safety guard")
    args = ap.parse_args()

    in_path = args.input
    out_path = args.output or in_path.with_suffix(".uvtt")

    if not in_path.is_file():
        print(f"❌ Input file not found: {in_path}")
        return

    print(f"Conversion started for '{in_path}'…")

    try:
        scene = _load_first_scene(in_path)
    except Exception as exc:
        print(f"❌ {exc}")
        return

    uvtt, dbg = convert_foundry_to_uvtt(scene, expand_map=not args.no_expand)

    # Short summary for eyeballing
    print("\n--- transform summary ---")
    for k, v in dbg.items():
        print(f"{k:24}: {v}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(uvtt, fh, indent=2)
        print(f"\n✅ Output written to '{out_path}'")
    except Exception as exc:
        print(f"❌ Failed to write '{out_path}': {exc}")


if __name__ == "__main__":
    main()
