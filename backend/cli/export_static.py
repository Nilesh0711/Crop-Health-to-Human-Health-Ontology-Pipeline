"""Export a pre-indexed static dataset for GitHub Pages without running any models."""
import argparse
import json
from pathlib import Path

from backend.config.paths import ONTOLOGY_DIR, PDF_DIR, PROJECT_ROOT
from backend.viewer.static_export import export_static_site


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ontology', type=Path, default=ONTOLOGY_DIR)
    parser.add_argument('--pdf-dir', type=Path, default=PDF_DIR)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / 'deploy/pages/data')
    parser.add_argument('--include-pdfs', action='store_true')
    parser.add_argument('--replace', action='store_true', help='Keep the previous static export locally and replace with a fresh bundle')
    args = parser.parse_args()
    try:
        result = export_static_site(args.ontology, args.pdf_dir, args.output, include_pdfs=args.include_pdfs, replace=args.replace)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Static export failed: {exc}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
