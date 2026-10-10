"""Check Git-tracked and untracked-not-ignored files for credentials."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_dots.security import Secrets
from hermes_dots.security.scanning import credential_hits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--secrets-file", help="Explicit 0600 env file whose values must also be checked")
    args = parser.parse_args()
    try:
        if args.secrets_file:
            Secrets().load_env(args.secrets_file)
        hits = list(credential_hits(Path(args.root).resolve()))
    except (OSError, ValueError):
        print("Credential scan could not complete safely", file=sys.stderr)
        return 2
    for hit in hits:
        print(hit)
    if not hits:
        print("Credential scan: clean")
    return int(bool(hits))


if __name__ == "__main__":
    sys.exit(main())
