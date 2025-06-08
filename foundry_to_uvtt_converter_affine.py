import json
import argparse
import pathlib
import math


def build_uvtt(scene_line: str,
               image_w_px: int,
               image_h_px: int,
               pixels_per_grid: int = 140,
               warn_tol: float = 1e-3):
    """
    Convert one Foundry scene line into a UVTT dict using an isotropic affine fit.
    • image_w_px / image_h_px  = dimensions of the PNG you'll import to Roll20
    • pixels_per_grid          = 140 for almost all commercial 5-e maps
    • warn_tol                 = relative error tolerance before we yell about aspect ratio
    """
    scene = json.loads(scene_line)

    # ------------------------------------------------------------------
    # 1) Gather raw wall vertices in Foundry-canvas pixels
    # ------------------------------------------------------------------
    wall_segments = scene.get("walls", [])
    # The 'walls' array contains objects; we need to extract the 'c' coordinate array.
    walls_raw_coords = [
        w.get('c') for w in wall_segments
        if isinstance(w, dict) and w.get('c') and isinstance(w.get('c'), list) and len(w.get('c')) == 4
    ]

    if not walls_raw_coords:
        print("Warning: No valid wall data found in the scene to process.")
        return {
            "format": 1.0,
            "resolution": {
                "map_size": {"x": image_w_px / pixels_per_grid, "y": image_h_px / pixels_per_grid},
                "pixels_per_grid": pixels_per_grid,
                "map_origin": {"x": 0, "y": 0}
            },
            "image": scene.get('background', {}).get('src', ''),
            "line_of_sight": [],
            "portals": []
        }

    xs = [p for w in walls_raw_coords for p in (w[0], w[2])]
    ys = [p for w in walls_raw_coords for p in (w[1], w[3])]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    raw_w, raw_h = max_x - min_x, max_y - min_y

    if raw_w < 1e-6:
        print("Error: Wall data has zero width. Cannot perform scaling.")
        return None

    # ------------------------------------------------------------------
    # 2) Single scale (gain) from X-span, then offsets that nail L/R edges
    # ------------------------------------------------------------------
    gain = image_w_px / raw_w
    off_x = -min_x * gain
    off_y = image_h_px - (max_y * gain)

    # ------------------------------------------------------------------
    # 3) Aspect-ratio sanity check (flags need for anisotropic scaling)
    # ------------------------------------------------------------------
    expect_h = raw_h * gain
    rel_err = abs(expect_h - image_h_px) / image_h_px if image_h_px > 0 else 0
    if rel_err > warn_tol:
        print(f"⚠ Aspect-ratio mismatch! "
              f"Expected img_h ≈ {expect_h:.2f}px but got {image_h_px}px "
              f"(error={rel_err:.3%}). Isotropic scaling will letterbox/pillarbox.")

    # ------------------------------------------------------------------
    # 4) Transform every wall → UVTT grid-units
    # ------------------------------------------------------------------
    to_grid = lambda p: p / pixels_per_grid
    line_of_sight = []
    for x1, y1, x2, y2 in walls_raw_coords:
        x1u = to_grid(gain * x1 + off_x)
        y1u = to_grid(gain * y1 + off_y)
        x2u = to_grid(gain * x2 + off_x)
        y2u = to_grid(gain * y2 + off_y)
        line_of_sight.append([{'x': x1u, 'y': y1u}, {'x': x2u, 'y': y2u}])

    # ------------------------------------------------------------------
    # 5) Compose UVTT payload
    # ------------------------------------------------------------------
    uvtt = {
        "format": 1.0,
        "resolution": {
            "map_size": {"x": image_w_px / pixels_per_grid, "y": image_h_px / pixels_per_grid},
            "pixels_per_grid": pixels_per_grid,
            "map_origin": {"x": 0, "y": 0}
        },
        "image": scene.get('background', {}).get('src', ''),
        "line_of_sight": line_of_sight,
        "portals": []  # Doors can be added later; focus on wall alignment first.
    }

    # ------------------------------------------------------------------
    # 6) Debug printout
    # ------------------------------------------------------------------
    print("\n--- Isotropic affine fit ---")
    print(f"raw_bbox  : ({min_x:.1f},{min_y:.1f}) – ({max_x:.1f},{max_y:.1f})  "
          f"[{raw_w:.1f} × {raw_h:.1f}] px")
    print(f"bitmap_px : {image_w_px} × {image_h_px}")
    print(f"gain      : {gain:.6f}  offs = ({off_x:.1f}, {off_y:.1f}) px")
    if rel_err <= warn_tol:
        print("✓ aspect ratio matches → perfect border fit")
    else:
        print(f"⚠ rendered height will differ by {abs(expect_h-image_h_px):.1f}px ({rel_err:.2%})")

    return uvtt


def main():
    parser = argparse.ArgumentParser(
        description="Convert Foundry VTT map data (.db/.json) to Universal VTT format (.uvtt) using an affine fit."
    )
    parser.add_argument(
        "input_file",
        type=pathlib.Path,
        help="Path to the source Foundry VTT .db or .json file."
    )
    parser.add_argument(
        "-o", "--output_file",
        type=pathlib.Path,
        help="Path to save the converted UVTT file. "
             "Defaults to the input filename with a .uvtt extension in the same directory."
    )
    parser.add_argument(
        '--image-width', type=int,
        help="Width (in pixels) of the target map image for Roll20. If not provided, it's inferred from the Foundry scene's canvas width."
    )
    parser.add_argument(
        '--image-height', type=int,
        help="Height (in pixels) of the target map image for Roll20. If not provided, it's inferred from the Foundry scene's canvas height."
    )

    args = parser.parse_args()

    input_path = args.input_file
    if not input_path.is_file():
        print(f"Error: Input file '{input_path}' not found or is not a file.")
        return

    output_path = args.output_file
    if not output_path:
        output_path = input_path.with_suffix(".uvtt")
    
    if output_path.parent and not output_path.parent.exists():
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"Error: Could not create output directory '{output_path.parent}': {e}")
            return

    print(f"Conversion started for '{input_path}'...")

    scene_json_line = None
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and 'walls' in data:
                        scene_json_line = line
                        print(f"Found scene data on line {line_number}.")
                        break
                except json.JSONDecodeError:
                    pass
        
        if scene_json_line is None:
            print(f"Error: Could not find a valid Foundry VTT scene object with a 'walls' array in '{input_path}'.")
            return

    except Exception as e:
        print(f"An error occurred while reading the input file '{input_path}': {e}")
        return

    temp_scene_data = json.loads(scene_json_line)
    
    image_w = args.image_width if args.image_width is not None else temp_scene_data.get('width')
    image_h = args.image_height if args.image_height is not None else temp_scene_data.get('height')
    
    if not image_w or not image_h:
        print("Error: Could not determine image dimensions for affine fit.")
        print("Please provide them using --image-width and --image-height, or ensure the source file contains 'width' and 'height' fields.")
        return

    pixels_per_grid = temp_scene_data.get('grid', {}).get('size', 140)

    uvtt_data = build_uvtt(
        scene_json_line,
        image_w_px=image_w,
        image_h_px=image_h,
        pixels_per_grid=pixels_per_grid
    )

    if not uvtt_data:
        print("Conversion failed. Please check warnings above.")
        return

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(uvtt_data, f, indent=2)
        print(f"\nConversion complete. Output saved to '{output_path}'")
    except Exception as e:
        print(f"An error occurred while writing the output file '{output_path}': {e}")


if __name__ == "__main__":
    main() 