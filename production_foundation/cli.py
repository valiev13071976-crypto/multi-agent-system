"""CLI for backup scheduling (operator/CI)."""

from __future__ import annotations

import argparse
import sys

from production_foundation.runtime import build_production_foundation_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Production foundation backup/restore")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("backup", help="Create backup")
    restore = sub.add_parser("restore", help="Restore backup to isolated target")
    restore.add_argument("--backup-dir", required=True)
    restore.add_argument("--target-dir", required=True)

    args = parser.parse_args(argv)
    rt = build_production_foundation_runtime()
    rt.service.initialize()

    if args.cmd == "backup":
        manifest = rt.service.run_backup()
        print(manifest.backup_id)
        return 0

    from production_foundation.restore import RestoreService

    svc = RestoreService(target_data_dir=args.target_dir)
    result = svc.restore(args.backup_dir)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
