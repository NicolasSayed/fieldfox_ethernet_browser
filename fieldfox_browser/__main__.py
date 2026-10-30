"""GUI and CLI entry points for independent FieldFox Ethernet file access."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys

from .client import BrowserError, FieldFoxClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse and download FieldFox folders over Ethernet")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="Open the Tkinter browser (also the default)")
    for name, help_text in [("idn", "Read the instrument identity"), ("ls", "List a remote folder"),
                            ("download", "Download a file or complete folder")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("host", help="Instrument IP or hostname")
        command.add_argument("--port", type=int, default=5025)
        command.add_argument("--timeout", type=float, default=10, help="Socket inactivity timeout in seconds")
        if name != "idn":
            command.add_argument("path", help="Absolute FieldFox path, e.g. [INTERNAL]:\\RecordPlayback\\GPSA")
        if name == "ls":
            command.add_argument("--json", action="store_true", help="Print structured catalogue JSON")
            command.add_argument("--raw", action="store_true", help="Print the original catalogue response")
        if name == "download":
            command.add_argument("--output", "-o", required=True, help="New local file or folder")
            command.add_argument("--recursive", action="store_true", help="Copy a whole folder, preserving its layout")
            command.add_argument("--max-file-bytes", type=int, default=1024**3)
            command.add_argument("--max-total-bytes", type=int, default=100*1024**3)
    args = parser.parse_args(argv)
    try:
        if args.command in {None, "gui"}:
            from .gui import main as gui_main
            return gui_main()
        client = FieldFoxClient(args.host, args.port, args.timeout)
        if args.command == "idn":
            print(client.identify())
        elif args.command == "ls":
            if args.raw:
                from .client import RemotePath
                with client.connection() as connection:
                    print(connection.text(f'MMEM:CAT? "{RemotePath.parse(args.path)}"'))
            else:
                listing = client.catalog(args.path)
                if args.json:
                    print(json.dumps(asdict(listing), indent=2, ensure_ascii=False))
                else:
                    print(f"Used: {listing.used_bytes:,} bytes; free: {listing.free_bytes:,} bytes")
                    for entry in listing.entries:
                        size = "" if entry.size is None else f"{entry.size:,}"
                        print(f"{'DIR ' if entry.is_dir else 'FILE'} {size:>14}  {entry.name}")
        elif args.recursive:
            result = client.download_folder(args.path, args.output, max_file_bytes=args.max_file_bytes,
                                            max_total_bytes=args.max_total_bytes)
            print(f"Downloaded {result['file_count']} files, {result['bytes']:,} bytes to {result['destination']}")
        else:
            result = client.download_file(args.path, args.output, max_bytes=args.max_file_bytes)
            print(f"Downloaded {result['bytes']:,} bytes; SHA-256 {result['sha256']}")
        return 0
    except (BrowserError, OSError) as error:
        print(f"fieldfox-browser: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
