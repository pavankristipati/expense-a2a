import json
import sys

# Prints audit.log as a readable table. Optional filter: python show_log.py manager
only = sys.argv[1] if len(sys.argv) > 1 else None

print(f"{'time (UTC)':<26} {'caller':<8} {'decision':<9} {'reasons':<34} expense")
for line in open("audit.log"):
    e = json.loads(line)
    caller = e.get("caller") or "-"
    if only and caller != only and e["event"] != only:
        continue
    if e["event"] == "reading":
        checks = ", ".join(e["checks"]) or "all good"
        print(f"{e['time']:<26} {caller:<8} {'READ':<9} {checks:<34} receipt {e['file']}")
        continue
    if e["event"] == "error":
        print(f"{e['time']:<26} {caller:<8} {'ERROR':<9} {e['error_type']:<34} task {e['task_id']}")
        continue
    if e["event"] == "refused":
        print(f"{e['time']:<26} {'-':<8} {'REFUSED':<9} {e['reason']:<34}")
        continue
    reasons = ", ".join(e["reasons"]) or "none"
    print(f"{e['time']:<26} {caller:<8} {e['decision']:<9} {reasons:<34} {e['expense']}")
