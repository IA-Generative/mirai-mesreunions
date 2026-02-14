#!/usr/bin/env python3
"""
Measure normalization impact between a source file and a normalized file.
Outputs LUFS/TP/LRA before/after plus deltas.
"""

import argparse
import json
import re
import subprocess
import sys


def run_loudnorm_measure(path: str, target_i: float = -16.0, target_tp: float = -1.5, target_lra: float = 11.0):
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", path,
        "-af", f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {path}: {proc.stderr[-400:]}")

    matches = re.findall(r"\{[\s\S]*?\}", proc.stderr or "")
    if not matches:
        raise RuntimeError(f"loudnorm json not found for {path}")
    data = json.loads(matches[-1])
    return {
        "i": float(data.get("input_i")),
        "tp": float(data.get("input_tp")),
        "lra": float(data.get("input_lra")),
        "thresh": float(data.get("input_thresh")),
    }


def main():
    parser = argparse.ArgumentParser(description="Measure normalization impact (before/after).")
    parser.add_argument("--source", required=True, help="Path to source/original file")
    parser.add_argument("--normalized", required=True, help="Path to normalized/transcoded file")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    source = run_loudnorm_measure(args.source)
    normalized = run_loudnorm_measure(args.normalized)

    target_i = -16.0
    source_dist = abs(source["i"] - target_i)
    normalized_dist = abs(normalized["i"] - target_i)
    result = {
        "target": {"i": target_i, "tp": -1.5, "lra": 11.0},
        "source": source,
        "normalized": normalized,
        "delta": {
            "i": round(normalized["i"] - source["i"], 2),
            "tp": round(normalized["tp"] - source["tp"], 2),
            "lra": round(normalized["lra"] - source["lra"], 2),
            "thresh": round(normalized["thresh"] - source["thresh"], 2),
        },
        "improvement_to_target_lufs": round(source_dist - normalized_dist, 2),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return

    print("=== Normalization Impact ===")
    print(f"Source:     I={source['i']} LUFS, TP={source['tp']} dBTP, LRA={source['lra']}")
    print(f"Normalized: I={normalized['i']} LUFS, TP={normalized['tp']} dBTP, LRA={normalized['lra']}")
    print(
        "Delta:      "
        f"I={result['delta']['i']} LU, TP={result['delta']['tp']} dB, LRA={result['delta']['lra']}"
    )
    print(f"Improvement to -16 LUFS target: {result['improvement_to_target_lufs']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
