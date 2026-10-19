"""Build an immutable HACS archive from tracked files of one fork commit."""

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

COMPONENT = "custom_components/tuya_local/"


def git(*args):
    # Arguments come from GitHub Actions metadata; never run a shell.
    return subprocess.check_output(["git", *args])  # noqa: S603, S607


def build_release(upstream_tag, upstream_sha, source_commit, output):
    """Version fork changes independently without modifying upstream tags/source."""
    source_commit = (
        git("rev-parse", "--verify", f"{source_commit}^{{commit}}").decode().strip()
    )
    git("merge-base", "--is-ancestor", upstream_sha, source_commit)
    revision = int(git("rev-list", "--count", f"{upstream_sha}..{source_commit}"))
    if revision < 1:
        raise ValueError("No fork changes to release")
    # Use git objects, not the working directory: untracked/private files and
    # local edits can never leak into a public release.
    with tarfile.open(
        fileobj=io.BytesIO(git("archive", source_commit, COMPONENT))
    ) as source:
        files = {
            member.name.removeprefix(COMPONENT): source.extractfile(member).read()
            for member in source.getmembers()
            if member.isfile()
        }
    manifest = json.loads(files["manifest.json"])
    base = manifest["version"]
    if not re.fullmatch(r"\d{4}\.\d+\.\d+", base):
        raise ValueError("Fork releases require a stable upstream calendar version")
    if upstream_tag != f"{base}-rel":
        raise ValueError("Upstream tag does not match the source manifest version")
    version = f"{base}.{revision}"
    manifest["version"] = version
    files["manifest.json"] = (json.dumps(manifest, indent=4) + "\n").encode()
    files["fork_release.json"] = (
        json.dumps(
            {
                "version": version,
                "upstream_tag": upstream_tag,
                "source_commit": source_commit,
                "sha256": {
                    path: hashlib.sha256(value).hexdigest()
                    for path, value in sorted(files.items())
                },
            },
            indent=2,
        )
        + "\n"
    ).encode()
    # Stable entry timestamps/order make a retry byte-for-byte reproducible.
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for path, value in sorted(files.items()):
            entry = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, value)
    output.mkdir(parents=True, exist_ok=True)
    (output / "tuya_local.zip").write_bytes(buffer.getvalue())
    (output / "version.txt").write_text(version + "\n")
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-tag", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    print(
        build_release(
            args.upstream_tag, args.upstream_sha, args.source_commit, args.output
        )
    )


if __name__ == "__main__":
    main()
