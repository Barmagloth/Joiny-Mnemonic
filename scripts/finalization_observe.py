from __future__ import annotations

import argparse
import json
from pathlib import Path

from joiny_mnemonic.finalization_observer import observe_finalizations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only grammar statistics for captured assistant Stop events."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=100)
    args = parser.parse_args()
    result = observe_finalizations(args.db, sample_limit=args.sample_limit)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
