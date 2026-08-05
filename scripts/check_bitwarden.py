"""Verify the dashboard can fetch its Linear key from Bitwarden.

Run from a REAL terminal (not through Claude) — `bw unlock` needs a TTY:

    cd ~/Code/status-dashboard && uv run python scripts/check_bitwarden.py

Exercises the same `credentials.load_into_env()` the app calls at startup, then
checks the key actually authenticates. Never prints the key: only its length,
the field names found on the item, and the Linear account it belongs to.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from status_dashboard import credentials  # noqa: E402

CONFIG = pathlib.Path.home() / ".config/status-dashboard/.env"


def check_linear_key(key: str) -> tuple[bool, str]:
    request = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": "{ viewer { email name } }"}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"network error: {exc.reason}"

    if "errors" in body:
        return False, str(body["errors"][0].get("message", ""))[:80]
    viewer = body.get("data", {}).get("viewer")
    if not viewer:
        return False, "no viewer in response"
    return True, f"{viewer['name']} <{viewer['email']}>"


def main() -> int:
    if CONFIG.exists():
        _ = load_dotenv(CONFIG)
        print(f"config: {CONFIG}")
    else:
        print(f"config: MISSING at {CONFIG}")
        return 1

    item = os.environ.get("LINEAR_BW_ITEM")
    field = os.environ.get("LINEAR_BW_FIELD") or "LINEAR_API_KEY"
    print(f"item:   {item}")
    print(f"field:  {field}")
    if not item:
        print("\nLINEAR_BW_ITEM is not set; nothing to fetch.")
        return 1

    print(f"vault:  {credentials.vault_status(os.environ.get('BW_SESSION'))}")

    # An inherited env var wins over Bitwarden, so say where the key came from.
    # Otherwise a stale value from `secrets-load` looks like a successful fetch.
    preexisting = os.environ.get("LINEAR_API_KEY")
    if preexisting:
        print(
            f"\nNOTE: LINEAR_API_KEY was already in the environment "
            f"({len(preexisting)} chars) — probably from `secrets-load`.\n"
            f"      Bitwarden will NOT be consulted while it is set."
        )

    print("\nFetching (you may be prompted for your master password)...")
    credentials.load_into_env()

    key = os.environ.get("LINEAR_API_KEY")
    source = "environment (not Bitwarden)" if preexisting else "Bitwarden"
    if not key:
        print("\nFAIL: LINEAR_API_KEY was not populated.")
        raw = credentials._bw(  # pyright: ignore[reportPrivateUsage]
            ["get", "item", item], os.environ.get("BW_SESSION")
        )
        if raw:
            names = [f.get("name") for f in (json.loads(raw).get("fields") or [])]
            print(f"Fields present on that item: {names}")
            print(f"Set LINEAR_BW_FIELD in {CONFIG} if the name differs.")
        return 1

    print(f"OK:     LINEAR_API_KEY present ({len(key)} chars), source: {source}")

    ok, detail = check_linear_key(key)
    print(f"{'OK' if ok else 'FAIL'}:   Linear API — {detail}")
    if not ok:
        print(
            "\nThat key is dead. Either the Bitwarden field still holds the old\n"
            "rotated key, or this shell exported a stale one before rotation.\n"
            "Update the field in Bitwarden, then run `secrets-load -f` to refresh."
        )
        return 1

    on_disk = CONFIG.read_text()
    leaked = "LINEAR_API_KEY=" in on_disk and not on_disk.count("# LINEAR_API_KEY")
    print(f"{'FAIL' if leaked else 'OK'}:   no Linear key written to disk")

    print("\nAll good — `uv run status-dashboard` will pick the key up the same way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
