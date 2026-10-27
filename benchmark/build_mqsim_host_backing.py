"""Build an isolated MQSim snapshot with disk-backed host mapping-table memory."""

import argparse
import subprocess
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--ref", default="b8a1dff35057714e6ad04e99df2675fc05344ea2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    archive = args.output / "source.tar"
    subprocess.run(["git", "-C", str(args.repository), "archive", args.ref,
                    "--output", str(archive.resolve())], check=True)
    with tarfile.open(archive) as source:
        source.extractall(args.output, filter="data")
    archive.unlink()
    patch = Path(__file__).with_name("mqsim-host-backing.patch").resolve()
    subprocess.run(["patch", "-p1", "-i", str(patch)], cwd=args.output, check=True)
    subprocess.run(["make", "-j", "2"], cwd=args.output, check=True)


if __name__ == "__main__":
    main()
