from pathlib import Path
import sys
import json
import argparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meridian.evaluation import run_fixture_evaluation


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate retrieval and exact-quote grounding against a labeled JSON case set")
    parser.add_argument("--cases", type=Path, default=ROOT / "data" / "evaluation_cases.json",
        help="JSON file containing named cases, passages, expected evidence IDs, and optional claims")
    args = parser.parse_args()
    result = run_fixture_evaluation(args.cases)
    print(json.dumps(result, indent=2))
