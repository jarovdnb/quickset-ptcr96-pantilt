import yaml
import os
from filelock import FileLock

home_dir = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("PANTILT_CONFIG_PATH", home_dir + "/pantilt_config.yaml")
LOCK_PATH = CONFIG_PATH + ".lock"

def retrieve_yaml_file():
    config = {}
    try:
        with FileLock(LOCK_PATH):
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, 'r') as file:
                    config = yaml.safe_load(file) or {}
    except Exception as e:
        print(f"⚠️ Error reading YAML file: {e}")
    return config

#   YAML functions
def update_yaml_flag(TAGlvl1, TAGlvl2, value):
    try:
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as f:
                config = yaml.safe_load(f) or {}
            if TAGlvl1 not in config or TAGlvl2 not in config[TAGlvl1]:
                raise KeyError(f"'{TAGlvl1}' or '{TAGlvl2}' not found in config")
            config[TAGlvl1][TAGlvl2] = value
            with open(CONFIG_PATH, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False)
    except Exception as e:
        print(f"⚠️ Failed to update config: {e}")

def update_yaml_flags(TAGlvl1, values):
    #   Same as update_yaml_flag but writes multiple keys of one section
    #   under a single lock acquisition
    try:
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as f:
                config = yaml.safe_load(f) or {}
            if TAGlvl1 not in config:
                raise KeyError(f"'{TAGlvl1}' not found in config")
            for TAGlvl2, value in values.items():
                if TAGlvl2 not in config[TAGlvl1]:
                    raise KeyError(f"'{TAGlvl1}' or '{TAGlvl2}' not found in config")
                config[TAGlvl1][TAGlvl2] = value
            with open(CONFIG_PATH, 'w') as f:
                yaml.safe_dump(config, f, default_flow_style=False)
    except Exception as e:
        print(f"⚠️ Failed to update config: {e}")

def ensure_yaml_section(TAGlvl1, defaults):
    #   Create a missing section and add missing keys (existing values are kept).
    #   Needed to migrate the config file of an already deployed unit.
    try:
        with FileLock(LOCK_PATH):
            with open(CONFIG_PATH, 'r') as f:
                config = yaml.safe_load(f) or {}
            section = config.setdefault(TAGlvl1, {})
            changed = False
            for key, value in defaults.items():
                if key not in section:
                    section[key] = value
                    changed = True
            if changed:
                with open(CONFIG_PATH, 'w') as f:
                    yaml.safe_dump(config, f, default_flow_style=False)
    except Exception as e:
        print(f"⚠️ Failed to ensure config section '{TAGlvl1}': {e}")
