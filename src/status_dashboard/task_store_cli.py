import argparse
import csv
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from status_dashboard.task_store import (
    HEADER,
    Rows,
    StoreError,
    TaskStore,
    decode_document,
    encode_document,
)


class Arguments(argparse.Namespace):
    command: str = ""
    path: Path = Path()


def csv_rows(path: Path) -> Rows:
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        if next(reader, list[str]()) != HEADER:
            raise StoreError("CSV header must match: " + ",".join(HEADER))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    _ = encode_document(rows)
    return rows


def main() -> None:
    config = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "status-dashboard/.env"
    )
    _ = load_dotenv(config if config.exists() else find_dotenv(usecwd=True))
    parser = argparse.ArgumentParser(
        description="Initialize, import, export, or restore the S3 task document."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser(
        "init-empty", help="Create an empty list only if the object does not exist"
    )
    importer = commands.add_parser(
        "import-csv", help="Create a list from a user-provided CSV; never overwrite"
    )
    _ = importer.add_argument("path", type=Path)
    _ = commands.add_parser(
        "check", help="Validate access and report only the task count"
    )
    exporter = commands.add_parser(
        "export", help="Save the current document to a new private local file"
    )
    _ = exporter.add_argument("path", type=Path)
    restore = commands.add_parser(
        "restore", help="Restore a downloaded JSON snapshot, archiving the current list"
    )
    _ = restore.add_argument("path", type=Path)
    _ = restore.add_argument("--confirm", action="store_true", required=True)
    args = Arguments()
    _ = parser.parse_args(namespace=args)
    try:
        store = TaskStore()
        if args.command == "init-empty":
            store.initialize([])
        elif args.command == "import-csv":
            store.initialize(csv_rows(args.path))
        elif args.command == "check":
            print(f"Task storage verified: {len(store.read().rows)} tasks.")
        elif args.command == "export":
            data = store.read().data
            descriptor = os.open(args.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                _ = file.write(data)
        elif args.command == "restore":
            rows = decode_document(args.path.read_bytes())

            def replace(current: Rows) -> bool:
                current[:] = rows
                return True

            _ = store.update(replace, attempts=1)
        print("Done.")
    except (StoreError, OSError) as error:
        parser.exit(
            1,
            f"{error if isinstance(error, StoreError) else 'Local file operation failed.'}\n",
        )


if __name__ == "__main__":
    main()
