"""Export a validated read-only publication without running any models."""
import argparse
import json
from pathlib import Path
from backend.config.paths import PROJECT_ROOT, ONTOLOGY_DIR, PDF_DIR
from backend.viewer.export import export_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ontology', type=Path, default=ONTOLOGY_DIR)
    parser.add_argument('--pdf-dir', type=Path, default=PDF_DIR)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / 'deploy/render/snapshot')
    parser.add_argument('--include-pdfs', action='store_true')
    parser.add_argument('--replace', action='store_true', help='Keep the previous snapshot locally and publish a checked replacement')
    args = parser.parse_args()
    try:
        result = export_snapshot(args.ontology, args.pdf_dir, args.output, include_pdfs=args.include_pdfs, replace=args.replace)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Viewer export failed: {exc}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
