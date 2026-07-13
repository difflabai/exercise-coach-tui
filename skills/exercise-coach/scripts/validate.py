#!/usr/bin/env python3
"""
Cassette validator — mechanically checks every invariant in the Workout
Cassette Format Specification. Run this on every generated cassette BEFORE
presenting it. A cassette that fails validation breaks mid-workout, which
is the worst possible place to discover a bug.

Usage:
    python validate.py workout.json

Exit codes: 0 = valid (warnings allowed), 1 = errors found, 2 = unreadable.

ERROR = will break or misbehave in the TUI. Must fix.
WARN  = degrades the experience (silence gaps, missing warmup). Should fix.
"""

import json
import sys

SUPPORTED_VERSIONS = {"1.1", "1.2"}
PHASE_TYPES = {"warmup", "main", "cooldown"}
GROUP_TYPES = {"straight", "superset", "circuit"}
MAX_CUE_GAP = 15  # seconds of silence tolerated during a timed hold

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def check_group(g, gi, phase_type, version):
    where = f"{phase_type} group[{gi}]"
    gtype = g.get("type")
    if gtype not in GROUP_TYPES:
        err(f"{where}: invalid type {gtype!r}")
        return

    rounds = g.get("rounds")
    if not isinstance(rounds, int) or rounds < 1:
        err(f"{where}: rounds must be a positive integer, got {rounds!r}")
        return

    exercises = g.get("exercises", [])
    n = len(exercises)
    if gtype == "straight" and n != 1:
        err(f"{where}: type 'straight' requires exactly 1 exercise, got {n}")
    elif gtype == "superset" and n != 2:
        err(f"{where}: type 'superset' requires exactly 2 exercises, got {n}")
    elif gtype == "circuit" and n < 3:
        err(f"{where}: type 'circuit' requires 3+ exercises, got {n}")

    if "rest" in g and (not isinstance(g["rest"], int) or g["rest"] < 0):
        err(f"{where}: rest must be a non-negative integer")

    timed_durations = {}  # round_index -> hold seconds (from the timed exercise)
    for ei, ex in enumerate(exercises):
        ename = ex.get("name", f"exercise[{ei}]")
        for field in ("name", "load"):
            if not isinstance(ex.get(field), str) or not ex.get(field):
                err(f"{where} {ename}: missing/empty '{field}'")
        if not isinstance(ex.get("timed"), bool):
            err(f"{where} {ename}: 'timed' must be boolean")
        sets = ex.get("sets")
        if not isinstance(sets, list):
            err(f"{where} {ename}: 'sets' must be an array")
            continue
        if len(sets) != rounds:
            err(f"{where} {ename}: sets length {len(sets)} != rounds {rounds}")
        for si, s in enumerate(sets):
            reps = s.get("reps") if isinstance(s, dict) else None
            if not isinstance(reps, int) or reps < 1:
                err(f"{where} {ename} set[{si}]: reps must be a positive integer")
        if ex.get("timed"):
            for si, s in enumerate(sets):
                if isinstance(s, dict) and isinstance(s.get("reps"), int):
                    timed_durations[si] = s["reps"]
        # v1.2 optional fields
        if "tempo" in ex:
            if version == "1.1":
                warn(f"{where} {ename}: 'tempo' is a v1.2 field on a v1.1 cassette")
            elif not isinstance(ex["tempo"], str):
                err(f"{where} {ename}: 'tempo' must be a string")
        if "per_side" in ex:
            if version == "1.1":
                warn(f"{where} {ename}: 'per_side' is a v1.2 field on a v1.1 cassette")
            elif not isinstance(ex["per_side"], bool):
                err(f"{where} {ename}: 'per_side' must be boolean")

    vrc = g.get("voice_round_complete")
    if vrc is not None:
        if not isinstance(vrc, list) or len(vrc) != rounds:
            err(f"{where}: voice_round_complete length "
                f"{len(vrc) if isinstance(vrc, list) else '?'} != rounds {rounds}")
        elif not all(isinstance(x, str) and x for x in vrc):
            err(f"{where}: voice_round_complete entries must be non-empty strings")

    vds = g.get("voice_during_set")
    has_timed = any(ex.get("timed") for ex in exercises)
    if has_timed and vds is None and phase_type == "main":
        warn(f"{where}: timed exercise with no voice_during_set — "
             f"silent holds feel broken")
    if vds is not None:
        if not has_timed:
            warn(f"{where}: voice_during_set present but no timed exercise")
        if not isinstance(vds, list) or len(vds) != rounds:
            err(f"{where}: voice_during_set needs one sub-array per round "
                f"({len(vds) if isinstance(vds, list) else '?'} != {rounds})")
        else:
            for ri, cues in enumerate(vds):
                duration = timed_durations.get(ri)
                prev = 0
                last_at = None
                for ci, cue in enumerate(cues):
                    at = cue.get("at_seconds")
                    line = cue.get("line")
                    if not isinstance(at, int) or at < 0:
                        err(f"{where} round {ri+1} cue[{ci}]: bad at_seconds {at!r}")
                        continue
                    if not isinstance(line, str) or not line:
                        err(f"{where} round {ri+1} cue[{ci}]: missing line")
                    if last_at is not None and at < last_at:
                        err(f"{where} round {ri+1}: cues not sorted by at_seconds")
                    if duration is not None and at >= duration:
                        err(f"{where} round {ri+1} cue[{ci}]: at_seconds {at} "
                            f">= hold duration {duration}")
                    if at - prev > MAX_CUE_GAP:
                        warn(f"{where} round {ri+1}: {at - prev}s silence gap "
                             f"before cue at {at}s (max {MAX_CUE_GAP}s)")
                    prev = at
                    last_at = at
                if duration is not None:
                    if not cues:
                        warn(f"{where} round {ri+1}: timed hold with zero cues")
                    elif duration - prev > MAX_CUE_GAP:
                        warn(f"{where} round {ri+1}: {duration - prev}s silence "
                             f"from last cue to end of hold")


def validate(cassette):
    version = cassette.get("version")
    if version not in SUPPORTED_VERSIONS:
        err(f"version {version!r} not in supported {sorted(SUPPORTED_VERSIONS)}")
        version = "1.2"  # keep checking with the permissive schema

    meta = cassette.get("meta")
    if not isinstance(meta, dict):
        err("missing 'meta' object")
    else:
        for field in ("date", "title"):
            if not isinstance(meta.get(field), str) or not meta.get(field):
                err(f"meta.{field} missing or empty")
        if not isinstance(meta.get("rest_default"), int):
            err("meta.rest_default missing or not an integer")

    phases = cassette.get("phases")
    if not isinstance(phases, list) or not phases:
        err("'phases' must be a non-empty array")
        phases = []
    if not any(p.get("type") == "warmup" and p.get("groups") for p in phases):
        warn("no warmup phase with exercises — warmup is non-negotiable "
             "for injury prevention")

    for pi, phase in enumerate(phases):
        ptype = phase.get("type")
        if ptype not in PHASE_TYPES:
            err(f"phase[{pi}]: invalid type {ptype!r}")
            ptype = f"phase[{pi}]"
        groups = phase.get("groups")
        if not isinstance(groups, list):
            err(f"phase[{pi}] ({ptype}): 'groups' must be an array (can be empty)")
            continue
        for gi, g in enumerate(groups):
            check_group(g, gi, ptype, version)

    for ci, cx in enumerate(cassette.get("context_exercises", [])):
        for field in ("name", "note"):
            if not isinstance(cx.get(field), str) or not cx.get(field):
                err(f"context_exercises[{ci}]: missing/empty '{field}'")

    voice = cassette.get("voice")
    if not isinstance(voice, dict):
        err("missing session-level 'voice' object")

    # TTS hygiene: no markdown/emoji artifacts in any voice string
    def scan_voice(obj, path, in_voice=False):
        if isinstance(obj, str):
            if in_voice:
                for bad in ("**", "##", "```", "|", "×"):
                    if bad in obj:
                        warn(f"{path}: voice line contains {bad!r} — TTS will "
                             f"read it literally: {obj[:60]!r}")
        elif isinstance(obj, dict):
            for k, v in obj.items():
                scan_voice(v, f"{path}.{k}",
                           in_voice or k.startswith("voice") or k == "line")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                scan_voice(v, f"{path}[{i}]", in_voice)

    scan_voice(cassette, "cassette")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python validate.py workout.json")
        sys.exit(2)
    try:
        with open(sys.argv[1]) as f:
            cassette = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"UNREADABLE: {e}")
        sys.exit(2)

    validate(cassette)

    for w in warnings:
        print(f"WARN:  {w}")
    for e in errors:
        print(f"ERROR: {e}")
    if errors:
        print(f"\nINVALID — {len(errors)} error(s), {len(warnings)} warning(s)")
        sys.exit(1)
    print(f"VALID — 0 errors, {len(warnings)} warning(s)")
    sys.exit(0)
