"""Causal, no-deletion request-end checkpoint bound for reporting only."""

import json
from pathlib import Path

import numpy as np


def calculate_upper_bound(manifest_path: Path, end_ns: int, requests: int):
    manifest = json.loads(manifest_path.read_text())
    hashes = np.memmap(manifest_path.parent / manifest["hashes_file"], dtype="<u8", mode="r")
    block = manifest["tokens_per_block"]
    checkpoints = set()
    count = tokens = restored = restored_requests = 0
    with (manifest_path.parent / manifest["requests_file"]).open() as index:
        for line in index:
            row = json.loads(line)
            if count == requests or row["timestamp_ns"] >= end_ns:
                break
            path = hashes[row["offset"]:row["offset"] + row["blocks"]]
            limit = min(row["input_tokens"] - 1, row.get("max_prefix_tokens", 2**64 - 1))
            hit = 0
            for i in range(min(len(path), limit // block) - 1, -1, -1):
                if int(path[i]) in checkpoints:
                    hit = (i + 1) * block
                    break
            count += 1
            tokens += row["input_tokens"]
            restored += hit
            restored_requests += bool(hit)
            # Lookup precedes publication: no request can reuse its own output.
            # A historical checkpoint also proves its complete FULL prefix was
            # published; under no deletion that entire prefix remains readable.
            if len(path):
                checkpoints.add(int(path[-1]))
    return {
        "requests": count, "input_tokens": tokens, "restored_tokens": restored,
        "restored_requests": restored_requests, "compute_tokens": tokens - restored,
        "joint_token_hit_rate": restored / tokens,
        "scope": "empty start, request-end checkpoints, no Drop/Trim, N-1 prefix bound",
        "hashes_sha256": manifest["hashes_sha256"], "index_sha256": manifest["index_sha256"],
    }
