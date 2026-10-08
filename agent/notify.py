"""Print an issue body (title on the first line) when this run produced new tickets.

Compares docs/data/state.json with the committed version; prints nothing if unchanged.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REL = "docs/data/state.json"


def main():
    new = json.loads((ROOT / REL).read_text()).get("tickets")
    try:
        old = json.loads(subprocess.check_output(["git", "show", f"HEAD:{REL}"], cwd=ROOT)).get("tickets")
    except subprocess.CalledProcessError:
        old = None
    if not new or (old and old["date"] == new["date"]):
        return
    print(f"New paper tickets for {new['date']}\n")
    print("The strategy rebalanced its simulated portfolio. Nothing was traded in any real account.")
    print("If you want these in your own account, review them and place them yourself.\n")
    print("| Symbol | Side | Target weight | Reference close |\n|---|---|---|---|")
    for t in new["items"]:
        print(f"| {t['symbol']} | {t['side'].upper()} | {t['target_weight']:.0%} | ${t['reference_close']:.2f} |")


if __name__ == "__main__":
    main()
