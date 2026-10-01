"""Import interview-notes.md into the scenario table. Safe to re-run (prompt is unique)."""
import re
import sys
from pathlib import Path

from . import db

CATEGORY_ALIASES = {
    "architecture": "architecture", "leadership": "leadership", "behavioral": "leadership",
    "exec-communication": "exec-communication", "exec communication": "exec-communication",
    "business": "exec-communication",
}
FIELD = re.compile(r"^(Type|Category|My answer|Notes):\s*(.*)$", re.I)


def parse(text):
    entries, cur, last_field = [], None, None
    for line in text.splitlines():
        if line.startswith("## Q:"):
            cur = {"prompt": line[5:].strip(), "type": "", "category": "", "my answer": "", "notes": ""}
            entries.append(cur)
            last_field = None
        elif cur is not None and line.startswith("#"):
            cur, last_field = None, None  # new section heading ends the entry
        elif cur is not None:
            m = FIELD.match(line)
            if m:
                last_field = m.group(1).lower()
                cur[last_field] = m.group(2).strip()
            elif line.strip() and last_field and not line.startswith("---"):
                cur[last_field] = (cur[last_field] + " " + line.strip()).strip()
    return [e for e in entries if e["prompt"]]


def import_file(conn, path):
    n = 0
    for e in parse(Path(path).read_text()):
        cat = CATEGORY_ALIASES.get(e["category"].lower().strip(), "")
        if not cat:
            continue  # unknown category: skip rather than guess
        source = "real" if e["type"].lower().startswith("real") else "invented"
        cur = conn.execute(
            "INSERT OR IGNORE INTO scenario(category, prompt, source, my_answer, notes) VALUES (?,?,?,?,?)",
            (cat, e["prompt"], source, e["my answer"], e["notes"]))
        n += cur.rowcount
    conn.commit()
    return n


if __name__ == "__main__":
    conn = db.connect()
    print(f"Imported {import_file(conn, sys.argv[1])} new scenarios.")
