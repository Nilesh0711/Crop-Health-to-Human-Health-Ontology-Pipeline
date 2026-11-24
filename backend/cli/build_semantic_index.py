"""Build persistent BGE dense semantic index over core non-taxon, non-ChEBI OBO classes."""
from __future__ import annotations

import argparse
from pathlib import Path
import time

from backend.ontology.semantic_index import build_semantic_index
from backend.ontology.inventory_policy import DATABASE


def main() -> None:
    default_dir = Path(__file__).resolve().parents[2] / 'analysis/single_stage/v4_pilot'
    parser = argparse.ArgumentParser(description='Build core_bge_vectors.npy for Stage 2 hybrid RRF retrieval.')
    parser.add_argument('--index-dir', type=Path, default=default_dir)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--device', type=str, default=None)
    args = parser.parse_args()

    t0 = time.perf_counter()
    result = build_semantic_index(args.index_dir / DATABASE, args.index_dir, batch_size=args.batch_size, device=args.device)
    elapsed = time.perf_counter() - t0
    print(f"Built semantic index in {elapsed:.1f}s on {result['device']}: "
          f"{result['count']} classes x {result['dim']}d -> {result['vectors_path']} "
          f"(sha256={result['vectors_sha256'][:16]}...)")


if __name__ == '__main__':
    main()
