"""
enrich_waterfall.py — Phase 2 Step 2: Apply validated patches to waterfall

Reads waterfall_patches from validation.db, lets you review/accept/reject
each suggestion, and writes:
  1. gaap_mappings_extended.json  — your extended version of edgartools' file
  2. Regenerates the _IS/_BS/_CF_WATERFALL sections in facts_processor.py

Usage
─────
    python enrich_waterfall.py --review          # interactive review mode
    python enrich_waterfall.py --auto-accept 0.7 # accept patches with conf >= 0.7
    python enrich_waterfall.py --apply           # apply accepted patches to facts_processor.py
    python enrich_waterfall.py --report          # show current patch status
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

OUTPUT_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase2_output")
DB_PATH      = os.path.join(OUTPUT_DIR, "validation.db")
EXTENDED_MAP = os.path.join(OUTPUT_DIR, "gaap_mappings_extended.json")
FP_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "facts_processor.py")


def load_db() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Run validate_pipeline.py first.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_extended_map() -> dict:
    """Load existing extended mappings or start fresh."""
    if os.path.exists(EXTENDED_MAP):
        with open(EXTENDED_MAP) as f:
            return json.load(f)
    return {
        "_meta": {
            "description": "Extended gaap_mappings — validated patches from Phase 2",
            "created": datetime.now().isoformat(timespec="seconds"),
            "source":  "enrich_waterfall.py",
        },
        "accepted": {},   # standard_tag → [raw_concepts]
        "rejected": {},   # standard_tag → [raw_concepts] (rejected, don't re-suggest)
    }


def save_extended_map(data: dict) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    data["_meta"]["last_updated"] = datetime.now().isoformat(timespec="seconds")
    with open(EXTENDED_MAP, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved: {EXTENDED_MAP}")


def report(conn: sqlite3.Connection) -> None:
    """Show current patch status."""
    ext = load_extended_map()
    accepted = ext.get("accepted", {})
    rejected = ext.get("rejected", {})

    patches = conn.execute("""
        SELECT standard_tag, raw_concept, statement, gap_count, concept_count, confidence
        FROM waterfall_patches
        ORDER BY gap_count DESC, confidence DESC
    """).fetchall()

    total    = len(patches)
    n_acc    = sum(1 for p in patches if p["raw_concept"] in accepted.get(p["standard_tag"], []))
    n_rej    = sum(1 for p in patches if p["raw_concept"] in rejected.get(p["standard_tag"], []))
    n_pending = total - n_acc - n_rej

    print(f"\nPatch status: {total} total  |  {n_acc} accepted  |  {n_rej} rejected  |  {n_pending} pending")
    print(f"\n{'─'*110}")
    print(f"{'Standard Tag':<50} {'Raw Concept':<55} {'gaps':>5} {'conf':>6}  {'Status'}")
    print(f"{'─'*110}")

    for p in patches:
        tag  = p["standard_tag"]
        conc = p["raw_concept"]
        if conc in accepted.get(tag, []):
            status = "✓ accepted"
        elif conc in rejected.get(tag, []):
            status = "✗ rejected"
        else:
            status = "? pending"
        print(f"{tag:<50} {conc:<55} {p['gap_count']:>5} {p['confidence']:>6.2f}  {status}")
    print(f"{'─'*110}\n")


def auto_accept(conn: sqlite3.Connection, min_confidence: float) -> None:
    """Automatically accept all patches above confidence threshold."""
    ext = load_extended_map()

    patches = conn.execute("""
        SELECT standard_tag, raw_concept, statement, gap_count, confidence
        FROM waterfall_patches
        WHERE confidence >= ?
        ORDER BY gap_count DESC
    """, (min_confidence,)).fetchall()

    accepted_count = 0
    for p in patches:
        tag  = p["standard_tag"]
        conc = p["raw_concept"]
        # Skip if already decided
        if conc in ext["accepted"].get(tag, []):
            continue
        if conc in ext["rejected"].get(tag, []):
            continue
        ext["accepted"].setdefault(tag, [])
        if conc not in ext["accepted"][tag]:
            ext["accepted"][tag].append(conc)
            accepted_count += 1
            print(f"  AUTO-ACCEPTED: {tag} ← {conc} (conf={p['confidence']:.2f}, gaps={p['gap_count']})")

    save_extended_map(ext)
    print(f"\nAuto-accepted {accepted_count} patches (confidence >= {min_confidence})")


def interactive_review(conn: sqlite3.Connection) -> None:
    """Interactive review of pending patches."""
    ext = load_extended_map()

    patches = conn.execute("""
        SELECT standard_tag, raw_concept, statement, gap_count, concept_count, confidence
        FROM waterfall_patches
        ORDER BY gap_count DESC, confidence DESC
    """).fetchall()

    pending = [
        p for p in patches
        if p["raw_concept"] not in ext["accepted"].get(p["standard_tag"], [])
        and p["raw_concept"] not in ext["rejected"].get(p["standard_tag"], [])
    ]

    if not pending:
        print("No pending patches to review.")
        report(conn)
        return

    print(f"\n{len(pending)} patches to review.")
    print("Commands: [a]ccept  [r]eject  [s]kip  [q]uit\n")

    for i, p in enumerate(pending, 1):
        tag  = p["standard_tag"]
        conc = p["raw_concept"]
        print(f"[{i}/{len(pending)}]  {tag}")
        print(f"  Raw concept : {conc}")
        print(f"  Statement   : {p['statement']}")
        print(f"  Gap count   : {p['gap_count']} tickers unresolved for this tag")
        print(f"  Filed by    : {p['concept_count']} of those tickers")
        print(f"  Confidence  : {p['confidence']:.2f}")

        while True:
            choice = input("  → [a/r/s/q]: ").strip().lower()
            if choice == "a":
                ext["accepted"].setdefault(tag, [])
                if conc not in ext["accepted"][tag]:
                    ext["accepted"][tag].append(conc)
                print(f"  ✓ Accepted")
                break
            elif choice == "r":
                ext["rejected"].setdefault(tag, [])
                if conc not in ext["rejected"][tag]:
                    ext["rejected"][tag].append(conc)
                print(f"  ✗ Rejected")
                break
            elif choice in ("s", ""):
                print(f"  → Skipped")
                break
            elif choice == "q":
                save_extended_map(ext)
                print("Saved and quit.")
                return
            else:
                print("  Invalid choice. Use a/r/s/q")
        print()

    save_extended_map(ext)
    print(f"\nReview complete. Saved to {EXTENDED_MAP}")


def apply_patches() -> None:
    """
    Read accepted patches from gaap_mappings_extended.json and inject them
    into facts_processor.py's waterfall lists.

    Strategy: for each accepted (standard_tag, raw_concept), append the
    raw_concept to the END of the existing concept list for that tag.
    This preserves current priority order (high-conf concepts first) and
    adds the validated additions as fallbacks.
    """
    if not os.path.exists(EXTENDED_MAP):
        print("No extended mappings found. Run --review or --auto-accept first.")
        return

    with open(EXTENDED_MAP) as f:
        ext = json.load(f)

    accepted = ext.get("accepted", {})
    if not accepted:
        print("No accepted patches to apply.")
        return

    with open(FP_PATH) as f:
        content = f.read()

    # Find each waterfall list and patch it
    changes = 0
    for tag, new_concepts in accepted.items():
        for concept in new_concepts:
            # Search for the tuple entry in any waterfall
            # Pattern: ("TAG", [  ...existing...  ], "UNIT"),
            pattern = rf'(\("{re.escape(tag)}",\s*\[)([^\]]*?)(\],\s*"(?:USD|shares|USD/shares|pure)")'

            def _inject(m):
                prefix  = m.group(1)
                body    = m.group(2)
                suffix  = m.group(3)
                # Only add if not already present
                if f'"{concept}"' in body:
                    return m.group(0)
                # Append before the closing bracket, preserving indentation
                last_quote = body.rfind('"')
                if last_quote == -1:
                    return m.group(0)
                insertion = f'\n        "{concept}",'
                new_body = body[:last_quote + 1] + insertion + body[last_quote + 1:]
                return prefix + new_body + suffix

            new_content = re.sub(pattern, _inject, content, flags=re.DOTALL)
            if new_content != content:
                content = new_content
                changes += 1
                print(f"  Patched: {tag} ← {concept}")

    with open(FP_PATH, "w") as f:
        f.write(content)

    print(f"\nApplied {changes} patches to {FP_PATH}")
    print("Run: python -m py_compile facts_processor.py  to verify")


def main():
    parser = argparse.ArgumentParser(description="Enrich waterfall from Phase 2 results")
    parser.add_argument("--review",       action="store_true",
                        help="Interactive review of pending patches")
    parser.add_argument("--auto-accept",  type=float, metavar="CONF",
                        help="Auto-accept patches with confidence >= CONF (e.g. 0.70)")
    parser.add_argument("--apply",        action="store_true",
                        help="Apply accepted patches to facts_processor.py")
    parser.add_argument("--report",       action="store_true",
                        help="Show current patch status")
    args = parser.parse_args()

    conn = load_db()

    if args.report or not any([args.review, args.auto_accept, args.apply]):
        report(conn)

    if args.auto_accept:
        auto_accept(conn, args.auto_accept)

    if args.review:
        interactive_review(conn)

    if args.apply:
        apply_patches()

    conn.close()


if __name__ == "__main__":
    main()
