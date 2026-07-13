#!/usr/bin/env python3
"""Build exercise-coach.skill from the skills/exercise-coach/ source tree.

The archive is built deterministically (sorted entries, fixed timestamps,
no permission bits that vary by umask, and stored uncompressed — deflate
output differs between zlib builds, e.g. zlib-ng vs zlib) so that rebuilding
from an unchanged tree is byte-identical on any machine. CI uses that
property to fail when the committed artifact is stale: rebuild, then
`git diff --exit-code exercise-coach.skill`.

Usage:
    python scripts/build_skill.py
"""

import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "skills" / "exercise-coach"
ARTIFACT = REPO / "exercise-coach.skill"

# Fixed timestamp for reproducible zips (2026-01-01 00:00:00).
FIXED_DATE = (2026, 1, 1, 0, 0, 0)


def main() -> int:
    if not SOURCE.is_dir():
        print(f"error: skill source tree not found at {SOURCE}", file=sys.stderr)
        return 1

    files = sorted(p for p in SOURCE.rglob("*") if p.is_file())
    with zipfile.ZipFile(ARTIFACT, "w", zipfile.ZIP_STORED) as zf:
        for path in files:
            arcname = f"exercise-coach/{path.relative_to(SOURCE).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE)
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())

    print(f"built {ARTIFACT.name} from {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
