"""Import a consented audience from CSV: address,channel,consent."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from config import load_settings
from store import Store


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 import_contacts.py opted_in_contacts.csv")
    path = Path(sys.argv[1])
    count = 0
    store = Store(load_settings().database_path)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            address = (row.get("address") or "").strip()
            channel = (row.get("channel") or "email").strip().lower()
            consent = (row.get("consent") or "").strip().lower()
            if not address or channel not in {"email", "sms"} or consent != "opted_in":
                continue
            store.set_consent(address, channel, "opted_in")
            count += 1
    store.audit("contacts_imported", {"count": count, "source": str(path)})
    print(f"imported {count} opted-in contacts")


if __name__ == "__main__":
    main()

