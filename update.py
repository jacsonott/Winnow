#!/usr/bin/env python3
"""Update Winnow in place, keeping everything you've done.

    python update.py --check                    what's available, change nothing
    python update.py                            check, show the plan, apply it
    python update.py --dry-run                  show the plan and stop
    python update.py --rollback                 undo the last update

Airgapped analysis box? Fetch on a machine that has network:

    python update.py --download-only --dest /media/usb

then carry it over and:

    python update.py --from /media/usb/winnow-1.1.0.zip

Your `workspace/` (case registry, saved filters, tag template, cases
folder), your installed `plugins/`, your `sessions/` and your case files
are never read, written or deleted by any of this — see updater.PROTECTED.
Stop the server before updating: Python has already loaded the old code,
so a running Winnow keeps running the old version until it restarts.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import updater
from updater import UpdateError

HERE = Path(__file__).resolve().parent


def _print_plan(plan: dict) -> None:
    print(f"  version:  {updater.installed_version()} -> {plan['version']}")
    for label, key in (("changed", "changed"), ("added", "added"), ("removed", "removed")):
        items = plan[key]
        if items:
            shown = ", ".join(items[:6]) + (f", +{len(items) - 6} more" if len(items) > 6 else "")
            print(f"  {label:8}: {len(items)} ({shown})")
    if plan["protected"]:
        print(f"  preserved: {' '.join(plan['protected'])}")
    if plan["first_update"]:
        print("  note:     first update on this install — files dropped by the new\n"
              "            version can't be identified yet, so none are removed.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="update.py", description="Update Winnow without losing settings, plugins or cases.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--check", action="store_true", help="report what's available and exit")
    p.add_argument("--dry-run", action="store_true", help="show exactly what would change, then stop")
    p.add_argument("--from", dest="from_file", metavar="ZIP",
                   help="apply a release archive already on this machine (the airgap path)")
    p.add_argument("--download-only", action="store_true",
                   help="fetch the latest release archive and exit, to carry to another machine")
    p.add_argument("--dest", metavar="DIR", default=".",
                   help="where --download-only writes (default: here)")
    p.add_argument("--rollback", action="store_true", help="restore the version before the last update")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    args = p.parse_args(argv)

    try:
        if args.rollback:
            res = updater.rollback(HERE)
            print(f"Rolled back to {res['version']} (from {res['backup']}).")
            print("Restart Winnow to run it.")
            return 0

        archive = Path(args.from_file) if args.from_file else None
        if archive is None:
            info = updater.check_for_update()
            if not info["available"]:
                print(f"Winnow {info['current']} is up to date "
                      f"(latest release is {info['latest']}).")
                return 0
            print(f"Winnow {info['latest']} is available (you have {info['current']}).")
            if info["notes"]:
                print("\n" + "\n".join("  " + ln for ln in info["notes"].splitlines()[:20]) + "\n")
            if args.check:
                print("Run `python update.py` to install it.")
                return 0
            dest = Path(args.dest) / f"winnow-{info['latest']}.zip"
            if args.download_only:
                out = updater.download(info["url"], dest)
                print(f"Downloaded {out} ({out.stat().st_size / 1e6:.1f} MB).")
                print(f"On the target machine: python update.py --from {out.name}")
                return 0
            print("Downloading…")
            archive = updater.download(info["url"], dest)
        elif args.check or args.download_only:
            p.error("--from can't be combined with --check or --download-only")

        plan = updater.plan_update(archive, HERE)
        print("This update will:")
        _print_plan(plan)
        if args.dry_run:
            print("\n--dry-run: nothing was changed.")
            return 0
        if not args.yes:
            if input("\nApply it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Cancelled — nothing was changed.")
                return 1
        res = updater.apply_update(archive, HERE)
        print(f"\nUpdated {res['previous_version']} -> {res['version']}.")
        print(f"Backup of the previous version: {res['backup']}")
        print("Undo with: python update.py --rollback")
        print("Restart Winnow to run the new version.")
        return 0
    except UpdateError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
