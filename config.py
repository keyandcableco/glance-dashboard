"""Load config.toml: named clocks and what each one watches. The first clock is the default."""
import os
import tomllib
from pathlib import Path

HERE = Path(__file__).parent
PATH = Path(os.environ.get("GLANCE_CONFIG", HERE / "config.toml"))


def load(path=PATH):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if not cfg.get("clocks"):
        raise SystemExit(f"{path}: no [clocks.<name>] sections")
    for name, clock in cfg["clocks"].items():
        if not clock.get("address"):
            raise SystemExit(f"{path}: clocks.{name} has no address")
        if "mail" in clock:
            clock["mail"]["inbox"] = os.path.expanduser(clock["mail"]["inbox"])
        if "calendar" in clock:
            clock["calendar"]["url_file"] = HERE / os.path.expanduser(clock["calendar"]["url_file"])
    return cfg


def default_address():
    """The first clock's address, for the one-shot tools (glance.py, set_setting.py)."""
    try:
        return next(iter(load()["clocks"].values()))["address"]
    except (OSError, SystemExit):
        return None
