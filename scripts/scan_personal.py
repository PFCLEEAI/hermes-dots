"""Check Git-tracked and untracked-not-ignored files for configured private terms."""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_dots.security.scanning import personal_hits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    terms_path = os.environ.get("HERMES_DOTS_PRIVATE_TERMS")
    if not terms_path or not Path(terms_path).is_file():
        print("Personal scan: no private terms file; nothing to check")
        return 0
    try:
        root = Path(args.root).resolve()
        if Path(terms_path).resolve().is_relative_to(root):
            print("Personal terms file must be outside the scanner root", file=sys.stderr)
            return 2
        terms = [line.strip() for line in Path(terms_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        hits = list(personal_hits(root, terms))
    except (OSError, UnicodeError, ValueError):
        print("Personal scan could not complete safely", file=sys.stderr)
        return 2
    for hit in hits:
        print(hit)
    if not hits:
        print("Personal scan: clean")
    return int(bool(hits))


if __name__ == "__main__":
    sys.exit(main())
