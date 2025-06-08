import json
import argparse
import pathlib

DEFAULT_PIXELS_PER_GRID = 140

# HACK: Manual offset to align with Roll20's grid interpretation.
# This shifts the map half a grid square west (-X) and north (-Y).
# In many graphics coordinate systems (like Roll20's), +Y is down, so a "north" shift is negative.
MANUAL_SHIFT_X_GRID = -0.75
MANUAL_SHIFT_Y_GRID = -0.5

def convert_foundry_to_uvtt(source_data):
    """
    Converts Foundry VTT map data (parsed from JSON) to Universal VTT format.
    """
    target_uvtt = {}

    # UVTT format version. Arkenforge examples and the sample UVTT file use 1.0.
    # The SOW defers to checking the importer or common values. 1.0 seems current.
    target_uvtt['format'] = 1.0

    scene_w = source_data.get('width', 0)
    scene_h = source_data.get('height', 0)

    # --- Extract grid properties ---
    grid_info = source_data.get('grid', {})
    if isinstance(grid_info, (int, float)):
        pixels_per_grid = grid_info or DEFAULT_PIXELS_PER_GRID
        grid_shift_x, grid_shift_y = 0.0, 0.0
    elif isinstance(grid_info, dict):
        pixels_per_grid = grid_info.get('size') or DEFAULT_PIXELS_PER_GRID
        grid_shift_x = grid_info.get('shiftX', 0.0) or 0.0
        grid_shift_y = grid_info.get('shiftY', 0.0) or 0.0
    else:  # Fallback for malformed or missing grid data
        pixels_per_grid = DEFAULT_PIXELS_PER_GRID
        grid_shift_x, grid_shift_y = 0.0, 0.0

    # --- Calculate offsets in pixels ---
    # 1. Padding offset
    padding = source_data.get('padding', 0.0)
    padding_offset_x = (padding or 0.0) * scene_w
    padding_offset_y = (padding or 0.0) * scene_h

    # 2. Background image offset
    bg = source_data.get('background', {})
    bg_center_x = float(bg.get('x', scene_w / 2.0) or (scene_w / 2.0))
    bg_center_y = float(bg.get('y', scene_h / 2.0) or (scene_h / 2.0))
    background_shift_x = bg_center_x - (scene_w / 2.0)
    background_shift_y = bg_center_y - (scene_h / 2.0)

    # 3. Total offset (Padding + Background shift + Grid shift)
    total_offset_x = padding_offset_x + background_shift_x + grid_shift_x
    total_offset_y = padding_offset_y + background_shift_y + grid_shift_y

    # Resolution block: map_size is in grid squares for UVTT.
    target_uvtt['resolution'] = {
        'map_origin': {'x': 0, 'y': 0},
        'map_size': {
            'x': scene_w / pixels_per_grid,
            'y': scene_h / pixels_per_grid
        },
        'pixels_per_grid': pixels_per_grid
    }

    # Image path/identifier
    target_uvtt['image'] = source_data.get('background', {}).get('src', '')

    # Initialize line_of_sight (for walls) and portals (for doors)
    target_uvtt['line_of_sight'] = []
    target_uvtt['portals'] = []

    source_walls = source_data.get('walls', [])
    if not isinstance(source_walls, list):
        print("Warning: 'walls' field is not a list or is missing. No walls or portals will be processed.")
        source_walls = []

    for i, wall_segment in enumerate(source_walls):
        if not isinstance(wall_segment, dict):
            print(f"Warning: Wall segment at index {i} is not a dictionary. Skipping.")
            continue

        coords = wall_segment.get('c', [])
        if not isinstance(coords, list) or len(coords) != 4:
            wall_id = wall_segment.get('_id', f'index {i}')
            print(f"Warning: Skipping wall segment '{wall_id}' due to invalid 'c' coordinates array.")
            continue

        try:
            # Convert coordinates to float as UVTT spec often implies float,
            # and calculations like midpoints will result in floats.
            x1_canvas = float(coords[0])
            y1_canvas = float(coords[1])
            x2_canvas = float(coords[2])
            y2_canvas = float(coords[3])

            # UVTT coordinates are in grid units, not pixels.
            # We convert from canvas pixel coordinates to grid coordinates.
            # Offsets for padding, background, and grid shift are subtracted before conversion.
            x1 = (x1_canvas - total_offset_x) / pixels_per_grid
            y1 = (y1_canvas - total_offset_y) / pixels_per_grid
            x2 = (x2_canvas - total_offset_x) / pixels_per_grid
            y2 = (y2_canvas - total_offset_y) / pixels_per_grid

            # Apply the manual grid-unit hack shift
            x1 += MANUAL_SHIFT_X_GRID
            y1 += MANUAL_SHIFT_Y_GRID
            x2 += MANUAL_SHIFT_X_GRID
            y2 += MANUAL_SHIFT_Y_GRID
        except (ValueError, TypeError) as e:
            wall_id = wall_segment.get('_id', f'index {i}')
            print(f"Warning: Skipping wall segment '{wall_id}' due to non-numeric coordinates: {e}")
            continue

        # Determine if the segment is a door (1) or a wall (0)
        # SOW: door: 0 for wall, 1 for door.
        is_door_val = wall_segment.get('door')
        if is_door_val is None: # door field might be missing
            is_door = False # Treat as wall if 'door' field is missing, or decide on a default.
            # print(f"Warning: 'door' field missing in wall segment '{wall_segment.get('_id', f'index {i}')}'. Treating as wall.")
        elif is_door_val == 1:
            is_door = True
        elif is_door_val == 0:
            is_door = False
        else:
            # print(f"Warning: Invalid value for 'door' field in wall segment '{wall_segment.get('_id', f'index {i}')}'. Treating as wall.")
            is_door = False # Treat as wall if 'door' is not 0 or 1.


        if not is_door:  # It's a standard wall
            uvtt_wall = [{'x': x1, 'y': y1}, {'x': x2, 'y': y2}]
            target_uvtt['line_of_sight'].append(uvtt_wall)
        else:  # It's a door
            mid_x = (x1 + x2) / 2.0
            mid_y = (y1 + y2) / 2.0

            # Door state (ds): 0 for closed, 1 for open (as per SOW interpretation)
            # SOW: "is_closed = wall_segment['ds'] == 0" and "Default to closed: true if unsure."
            door_state_ds = wall_segment.get('ds', 0)  # Default to 0 (closed) if 'ds' is missing
            is_closed = (door_state_ds == 0)

            uvtt_portal = {
                'position': {'x': mid_x, 'y': mid_y},
                'bounds': [{'x': x1, 'y': y1}, {'x': x2, 'y': y2}],
                'closed': is_closed,
                'freestanding': False  # SOW: Default this to False
            }
            target_uvtt['portals'].append(uvtt_portal)
            
    # As per SOW, only specific fields are mapped. 'lights', 'environment', 
    # 'objects_line_of_sight' are not requested for generation from Foundry data.

    # Collect debug info
    debug_info = {
        'Scene Dimensions (Canvas)': f"{scene_w} x {scene_h} pixels",
        'Scene Dimensions (Grid Squares)': f"{scene_w / pixels_per_grid:.2f} x {scene_h / pixels_per_grid:.2f}",
        'Padding Value': padding,
        'Grid Size (pixels_per_grid)': pixels_per_grid,
        'Padding Offset (x, y)': (padding_offset_x, padding_offset_y),
        'Background Shift (x, y)': (background_shift_x, background_shift_y),
        'Grid Shift (x, y)': (grid_shift_x, grid_shift_y),
        'Total Offset Subtracted (x, y) IN PIXELS': (total_offset_x, total_offset_y),
        'HACKED Manual Shift (x, y) IN GRID UNITS': (MANUAL_SHIFT_X_GRID, MANUAL_SHIFT_Y_GRID),
        'Walls Found': len(target_uvtt['line_of_sight']),
        'Portals (Doors/Windows) Found': len(target_uvtt['portals'])
    }

    return target_uvtt, debug_info

def main():
    parser = argparse.ArgumentParser(
        description="Convert Foundry VTT map data (.db/.json) to Universal VTT format (.uvtt)."
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

    args = parser.parse_args()

    input_path = args.input_file
    if not input_path.is_file():
        print(f"Error: Input file '{input_path}' not found or is not a file.")
        return

    output_path = args.output_file
    if not output_path:
        output_path = input_path.with_suffix(".uvtt")
    
    # Ensure output directory exists if a full path is given
    if output_path.parent and not output_path.parent.exists():
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"Error: Could not create output directory '{output_path.parent}': {e}")
            return


    print(f"Conversion started for '{input_path}'...")

    source_json_data = None
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            for line_number, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    # Check for essential scene keys to identify the map document
                    if isinstance(data, dict) and \
                       'width' in data and \
                       'height' in data and \
                       'grid' in data and isinstance(data.get('grid'), dict) and 'size' in data['grid'] and \
                       'background' in data and isinstance(data.get('background'), dict) and 'src' in data['background'] and \
                       'walls' in data: # 'walls' is crucial for conversion
                        source_json_data = data
                        print(f"Found scene data on line {line_number}.")
                        break # Found the scene data, assume it's the first valid one
                except json.JSONDecodeError:
                    # This line is not a valid JSON object or not the one we're looking for.
                    # print(f"Skipping line {line_number}, not valid JSON or not a scene: {line[:100]}...")
                    pass # Continue to the next line
        
        if source_json_data is None:
            print(f"Error: Could not find a valid Foundry VTT scene object in '{input_path}'.")
            print("Ensure the file contains a JSON object with 'width', 'height', 'grid.size', 'background.src', and 'walls' fields.")
            return

    except FileNotFoundError:
        print(f"Error: Input file '{input_path}' not found.") # Should be caught by earlier check
        return
    except Exception as e:
        print(f"An error occurred while reading or processing the input file '{input_path}': {e}")
        return

    # Perform the conversion
    uvtt_data, debug_stats = convert_foundry_to_uvtt(source_json_data)

    print("\n--- Extracted Debug Information ---")
    for key, value in debug_stats.items():
        print(f"{key:<35}: {value}")
    print("-----------------------------------\n")

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(uvtt_data, f, indent=4) # Use indent=4 for readable JSON output
        print(f"Conversion complete. Output saved to '{output_path}'")
    except Exception as e:
        print(f"An error occurred while writing the output file '{output_path}': {e}")

if __name__ == "__main__":
    main() 