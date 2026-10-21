"""Reflect menu-watch.toml's JST schedule in the GitHub Actions workflow."""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from menu_watch import WatchError, load_settings  # noqa: E402


def sync_schedule(config, workflow, check=False):
    settings = load_settings(config)
    text = workflow.read_text(encoding="utf-8")
    updated, count = re.subn(r"(?m)^(\s*- cron: )'[^'\n]+'$",
                            lambda match: match[1] + "'" + settings.cron_utc + "'", text)
    if count != 1:
        raise WatchError("Expected exactly one workflow cron entry")
    if check and text != updated:
        raise WatchError("Schedule differs from settings; run python3 scripts/update_schedule.py")
    if not check and text != updated:
        workflow.write_text(updated, encoding="utf-8")
    return settings.cron_utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        cron = sync_schedule(ROOT / "menu-watch.toml",
                             ROOT / ".github/workflows/notify-bampire.yml", args.check)
    except (WatchError, OSError) as exc:
        print(str(exc) if isinstance(exc, WatchError) else "Cannot read schedule files", file=sys.stderr)
        return 1
    print(f"Workflow schedule: {cron} (UTC)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
