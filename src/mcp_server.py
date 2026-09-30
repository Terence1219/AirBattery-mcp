"""MCP server that exposes AirBattery's battery readings to AI agents."""

import json
import os
import shutil
import subprocess
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# AirBattery installs its CLI as a symlink at /usr/local/bin/airbattery (Settings > Command Line Tool).
# The same binary also ships inside the app bundle, so users don't have to install the CLI at all.
CLI_CANDIDATES = [
    "/usr/local/bin/airbattery",
    "/Applications/AirBattery.app/Contents/Resources/abt",
    str(Path.home() / "Applications/AirBattery.app/Contents/Resources/abt"),
]
STATUS = {"+": "charging", "-": "discharging", "=": "paused", "?": "unknown"}
STALE_AFTER_MINUTES = 10  # AirBattery itself flags devices as possibly offline after 10 minutes
TIMEOUT_SECONDS = 15

mcp = MCPServer("airbattery")


def find_cli() -> str | None:
    override = os.environ.get("AIRBATTERY_CLI")
    for path in ([override] if override else []) + CLI_CANDIDATES:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return shutil.which("airbattery")


def parse_output(output: str) -> list[dict]:
    """Validate `airbattery --json` output and convert it into agent-friendly records."""
    items = json.loads(output)
    if not isinstance(items, list):
        raise ValueError("Expected a JSON array of devices.")

    devices = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Device at index {index} must be an object.")
        required = {"device", "level", "status", "update"}
        if not required.issubset(item):
            missing = ", ".join(sorted(required - item.keys()))
            raise ValueError(f"Device at index {index} is missing fields: {missing}.")
        if not isinstance(item["device"], str) or not item["device"].strip():
            raise ValueError(f"Device at index {index} must have a non-empty name.")
        # bool is a subclass of int in Python, but JSON booleans are not battery readings.
        if type(item["level"]) is not int or not 0 <= item["level"] <= 100:
            raise ValueError(f"Device at index {index} must have an integer level from 0 to 100.")
        if not isinstance(item["status"], str):
            raise ValueError(f"Device at index {index} must have a string status.")
        if type(item["update"]) is not int:
            raise ValueError(f"Device at index {index} must have an integer update value.")

        minutes = max(0, -item["update"])  # AirBattery reports (update time - now), i.e. negative
        devices.append({
            "device": item["device"],
            "level_percent": item["level"],
            "status": STATUS.get(item["status"], "unknown"),
            "minutes_since_update": minutes,
            "stale": minutes > STALE_AFTER_MINUTES,
        })
    return devices


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_battery_status(device: str | None = None, include_nearcast: bool = False) -> dict:
    """Get current battery levels of the user's devices (AirPods, iPhone, mice, headphones, speakers...).

    Data comes from the AirBattery app on this Mac. Each entry has:
    - level_percent: battery level 0-100
    - status: charging / discharging / paused / unknown
    - minutes_since_update: how old the reading is
    - stale: true when older than 10 minutes. A stale device is probably disconnected, so its level is
      the last known value, not the current one. Say so when answering.

    Non-Apple devices only report while connected to this Mac, and some report coarse levels,
    so treat small differences cautiously.

    Args:
        device: optional case-insensitive name filter, e.g. "airpods" or "mx master".
        include_nearcast: also include devices reported by other Macs on the LAN via AirBattery Nearcast.
    """
    cli = find_cli()
    if cli is None:
        return {"error": "AirBattery was not found. Install it from https://github.com/lihaoyun6/AirBattery "
                         "or set AIRBATTERY_CLI to the path of its command line tool."}

    args = [cli, "--json"] + (["--nearcast"] if include_nearcast else [])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"error": f"AirBattery did not respond within {TIMEOUT_SECONDS} seconds."}

    except OSError as exc:
        return {"error": "Could not execute the AirBattery command line tool.", "details": str(exc)[:500]}
    except UnicodeError:
        return {"error": "AirBattery returned output that could not be decoded as text."}

    if result.returncode != 0:
        return {
            "error": "AirBattery command line tool failed.",
            "exit_code": result.returncode,
            "stderr": result.stderr.strip()[:500],
        }

    try:
        devices = parse_output(result.stdout)
    except ValueError as exc:
        return {"error": "Unexpected output from AirBattery.", "details": str(exc)[:500]}

    if device:
        matched = [d for d in devices if device.lower() in d["device"].lower()]
        if not matched:
            return {"error": f"No device matching '{device}'.",
                    "available_devices": [d["device"] for d in devices]}
        devices = matched

    return {"devices": devices}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
