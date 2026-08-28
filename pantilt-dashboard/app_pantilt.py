from flask import Flask, render_template, request, jsonify, Response
import yaml
import json
import os
import re
from datetime import datetime
from pathlib import Path
from filelock import FileLock
import sys
sys.path.append(os.path.abspath(".."))
from lib.socket_helper import get_pantilt_socket_info
from lib import pantilt_program
from lib import keepout

# No login: this dashboard has no auth layer, so only run it on a network you
# trust (e.g. behind your own VPN/reverse-proxy auth) -- don't expose it
# directly to the internet.
app = Flask(__name__)

home_dir = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("PANTILT_CONFIG_PATH", home_dir + "/pantilt_config.yaml")
LOCK_PATH = CONFIG_PATH + ".lock"

#   Keep-out profile files (see lib/keepout.py) are written offline by
#   pt_keepout_record.py, one per instrument/mount -- this just lists what's
#   on disk so the dashboard can offer them in a dropdown.
KEEPOUT_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "keepout"))

@app.route('/')
def protected():
    return index()

def index():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as file:
                config = yaml.safe_load(file) or {}
        return render_template('index.html', config=config, keepout_profiles=keepout.list_profiles(KEEPOUT_DIR))

def load_config():
    with FileLock(LOCK_PATH):
        with open(CONFIG_PATH, 'r') as file:
            return yaml.safe_load(file)

def write_config(config):
    with FileLock(LOCK_PATH):
        with open(CONFIG_PATH, 'w') as file:
            yaml.dump(config, file)

def deep_update(source, overrides):
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(source.get(key), dict):
            deep_update(source[key], value)
        else:
            source[key] = value

def merge_config(overrides):
    # Read-modify-write under ONE lock so concurrent flag updates from
    # pantilt.py are never lost
    with FileLock(LOCK_PATH):
        try:
            with open(CONFIG_PATH, 'r') as file:
                current_config = yaml.safe_load(file) or {}
        except FileNotFoundError:
            current_config = {}

        deep_update(current_config, overrides)

        with open(CONFIG_PATH, 'w') as file:
            yaml.dump(current_config, file)

@app.route('/save_config', methods=['POST'])
def save_config():
    data = request.json
    try:
        merge_config(data)

        return jsonify({"status": "success", "message": "Config saved."})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/get_config', methods=['GET'])
def get_config():
    config = load_config()
    return jsonify(config)

# Uploaded program files are stored in pantilt/programs (pantilt.py's working directory)
PROGRAMS_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "programs"))

# Live pan-tilt state, streamed by pantilt.py on its own unix socket
@app.route('/get_pantilt_vars', methods=['GET'])
def get_pantilt_vars():
    data = get_pantilt_socket_info()
    return jsonify(data)

# Validate an uploaded program file and return the overview; nothing is saved
@app.route('/pantilt/preview_program', methods=['POST'])
def pantilt_preview_program():
    text = request.get_data(as_text=True)
    config = load_config() or {}

    try:
        return jsonify(pantilt_program.preview(text, config.get('pantilt', {})))
    except pantilt_program.ProgramError as e:
        return jsonify({"status": "error", "message": str(e)})

# Re-validate, save the program with a timestamp suffix and hand it to pantilt.py.
@app.route('/pantilt/run_program', methods=['POST'])
def pantilt_run_program():
    text = request.get_data(as_text=True)
    config = load_config() or {}
    pantilt_cfg = config.get('pantilt', {})

    try:
        result = pantilt_program.preview(text, pantilt_cfg)
    except pantilt_program.ProgramError as e:
        return jsonify({"status": "error", "message": str(e)})

    if not result["valid"]:
        return jsonify({"status": "error", "message": "Program is invalid, fix the errors first."})

    # Never trust the program name as a path
    name = re.sub(r'[^A-Za-z0-9_-]', '', result["summary"]["name"]) or "program"
    filename = f"{name}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.yaml"

    # Backfill whatever setup fields the upload didn't declare, from the
    # current config, so the saved copy fully documents what it ran under
    # and a later re-run reproduces the exact same conditions.
    prog = pantilt_program.load_program(text)
    pantilt_program.backfill_recorded_setup(prog, pantilt_cfg)
    text_to_save = pantilt_program.dump_program(prog)

    Path(PROGRAMS_DIR).mkdir(parents=True, exist_ok=True)
    program_path = os.path.join(PROGRAMS_DIR, filename)
    with open(program_path, 'w') as file:
        file.write(text_to_save)

    merge_config({"pantilt": {"run_program": program_path}})

    return jsonify({"status": "success", "message": f"Program saved as {filename} and started."})

# List previously saved/uploaded programs, for the dashboard's file dropdown
@app.route('/pantilt/list_programs', methods=['GET'])
def pantilt_list_programs():
    if not os.path.isdir(PROGRAMS_DIR):
        return jsonify([])
    files = sorted(f for f in os.listdir(PROGRAMS_DIR) if f.lower().endswith(('.yaml', '.yml')))
    return jsonify(files)

# Serve the raw contents of a saved program by name (basename only, no path traversal)
@app.route('/pantilt/program_content', methods=['GET'])
def pantilt_program_content():
    name = os.path.basename(request.args.get('name', ''))
    program_path = os.path.join(PROGRAMS_DIR, name)
    if not name or not os.path.isfile(program_path):
        return jsonify({"status": "error", "message": "Unknown program file"}), 404
    with open(program_path, 'r') as file:
        return Response(file.read(), mimetype='text/plain')

#   ---------------------------------------------------------------------
#   Keep-out envelope editor ("new window" opened from Advanced Settings):
#   visualizes the active/any profile on a sphere, jogs the live positioner
#   through the same one-shot flags the main jog controls use, and reads/
#   writes config/keepout/<name>.json directly (no daemon changes needed --
#   recording a sample only needs the live position + pan/tilt_invert,
#   both already exposed via get_pantilt_vars/get_config).
#   ---------------------------------------------------------------------

def _keepout_sanitized_name(name):
    #   Basename only, and restrict to a safe charset -- this becomes a
    #   filename (see keepout.profile_path)
    name = os.path.basename((name or "").strip())
    return name if re.match(r'^[A-Za-z0-9_-]+$', name) else ""

def _keepout_load_samples(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []

def _keepout_write_samples(path, samples):
    os.makedirs(KEEPOUT_DIR, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(samples, f, indent=2)

@app.route('/pantilt/keepout')
def pantilt_keepout_page():
    #   Profile list/samples/live status are all fetched client-side (see
    #   keepout.html) -- this route just serves the page shell.
    return render_template('keepout.html')

@app.route('/pantilt/keepout/profiles', methods=['GET'])
def pantilt_keepout_profiles():
    return jsonify(keepout.list_profiles(KEEPOUT_DIR))

# Raw boundary samples for one profile, as recorded -- the client buckets
# them into breakpoints itself (same model as lib/keepout.py) for the table/sphere
@app.route('/pantilt/keepout/profile/<name>', methods=['GET'])
def pantilt_keepout_get_profile(name):
    name = _keepout_sanitized_name(name)
    if not name:
        return jsonify({"status": "error", "message": "Invalid profile name"}), 400
    path = keepout.profile_path(KEEPOUT_DIR, name)
    return jsonify({"name": name, "samples": _keepout_load_samples(path)})

@app.route('/pantilt/keepout/profile/<name>', methods=['DELETE'])
def pantilt_keepout_delete_profile(name):
    name = _keepout_sanitized_name(name)
    if not name:
        return jsonify({"status": "error", "message": "Invalid profile name"}), 400
    path = keepout.profile_path(KEEPOUT_DIR, name)
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"status": "success", "message": f"Deleted profile '{name}'."})

# Full replace -- used by "save table to profile" and "import" in the editor.
# Recording (below) writes straight to the file instead, so a browser crash
# mid-mapping session never loses already-recorded points.
@app.route('/pantilt/keepout/profile/<name>/samples', methods=['POST'])
def pantilt_keepout_save_samples(name):
    name = _keepout_sanitized_name(name)
    if not name:
        return jsonify({"status": "error", "message": "Invalid profile name"}), 400

    data = request.json or {}
    clean = []
    for s in data.get('samples', []):
        try:
            clean.append({"tilt_deg": round(float(s["tilt_deg"]), 2), "pan_deg": round(float(s["pan_deg"]), 2)})
        except (KeyError, TypeError, ValueError):
            continue

    path = keepout.profile_path(KEEPOUT_DIR, name)
    _keepout_write_samples(path, clean)
    return jsonify({"status": "success", "message": f"Saved {len(clean)} sample(s) to {name}.json",
                     "samples": clean})

# Append ONE sample at the CURRENT live position (raw hardware angle, same
# frame pt_keepout_record.py and lib/keepout.py use -- undo the pan/tilt_invert
# sign flip pantilt.py's flip() applies for the logical pan_abs/tilt_abs the
# dashboard shows).
@app.route('/pantilt/keepout/profile/<name>/record', methods=['POST'])
def pantilt_keepout_record(name):
    name = _keepout_sanitized_name(name)
    if not name:
        return jsonify({"status": "error", "message": "Invalid profile name"}), 400

    live = get_pantilt_socket_info()
    if not live.get("connected"):
        return jsonify({"status": "error", "message": "Positioner not connected -- cannot record the current position."}), 400

    pantilt_cfg = (load_config() or {}).get('pantilt', {})
    pan_abs = float(live.get("pan_abs", 0.0))
    tilt_abs = float(live.get("tilt_abs", 0.0))
    raw_pan = -pan_abs if pantilt_cfg.get("pan_invert", 0) == 1 else pan_abs
    raw_tilt = -tilt_abs if pantilt_cfg.get("tilt_invert", 0) == 1 else tilt_abs

    path = keepout.profile_path(KEEPOUT_DIR, name)
    samples = _keepout_load_samples(path)
    sample = {"tilt_deg": round(raw_tilt, 2), "pan_deg": round(raw_pan, 2)}
    samples.append(sample)
    _keepout_write_samples(path, samples)

    return jsonify({"status": "success",
                     "message": f"Recorded tilt={sample['tilt_deg']:+.2f}° pan={sample['pan_deg']:+.2f}° "
                                f"({len(samples)} total).",
                     "sample": sample, "samples": samples})

@app.route('/pantilt/keepout/profile/<name>/undo', methods=['POST'])
def pantilt_keepout_undo(name):
    name = _keepout_sanitized_name(name)
    if not name:
        return jsonify({"status": "error", "message": "Invalid profile name"}), 400
    path = keepout.profile_path(KEEPOUT_DIR, name)
    samples = _keepout_load_samples(path)
    if not samples:
        return jsonify({"status": "error", "message": "Nothing to undo."}), 400
    removed = samples.pop()
    _keepout_write_samples(path, samples)
    return jsonify({"status": "success", "message": f"Removed tilt={removed['tilt_deg']:+.2f}° pan={removed['pan_deg']:+.2f}°.",
                     "samples": samples})

# Run the webserver
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
