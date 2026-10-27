"""Convert one finite hybrid replay and execute its complete MQSim I/O stream."""

import argparse
import json
from pathlib import Path

from dwpdsim.hybrid_mqsim import (
    convert_hybrid_trace,
    run_device,
    write_device_configuration,
    write_workload,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--capacity-tb", type=int, choices=(1, 4), required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--convert-only", action="store_true")
    parser.add_argument("--run-only", action="store_true")
    args = parser.parse_args()
    assert not (args.convert_only and args.run_only)
    replay = json.loads((args.replay / "result.json").read_text())
    device = args.replay / "device"
    if args.run_only:
        manifest = json.loads((device / "manifest.json").read_text())
    else:
        manifest = convert_hybrid_trace(Path(replay["trace_path"]), replay["config"], device)
        for pool in ("slc", "tlc"):
            reported = replay["stats"]["storage"][pool]
            for operation in ("read", "write", "trim"):
                assert manifest["pools"][pool].get(operation.upper()+"_bytes", 0) == reported[operation+"_bytes"]
        root = Path(__file__).resolve().parents[1]
        write_device_configuration(root / "example/mqsim/ssdconfig.xml", device / "ssd.xml",
                                   replay["config"], args.capacity_tb)
        write_workload(manifest, device / "workload.xml")
        print(json.dumps({"phase": "converted", "commands": manifest["commands"],
                          "source_events": manifest["source_events"]}), flush=True)
    if not args.convert_only:
        result = run_device(args.binary, device, manifest)
        print(json.dumps(result, indent=2), flush=True)
        if result["status"] != "completed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
