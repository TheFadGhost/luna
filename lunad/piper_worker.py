"""Persistent piper synthesiser. Runs under the project venv, not lunad's python.

lunad itself is stock system python; piper, onnxruntime and numpy live only in
``~/Work/luna/.venv``. So this file is never imported by the daemon — it is
executed as a script by ``config.VENV_PYTHON`` and spoken to over pipes.

Why a persistent worker at all: loading the model costs 1.12 s and 331 MB. One
process per sentence would pay that every sentence; one process for the whole
daemon lifetime would hold 331 MB against 3-4 GB of headroom. So the parent
keeps this alive while speech is happening and kills it after five idle
minutes (ARCHITECTURE.md section 5).

Wire format
-----------
stdin: one JSON object per line.
    {"op": "say", "id": "<str>", "sentences": ["...", ...]}
    {"op": "cancel"}          cancel whatever is being synthesised now
    {"op": "quit"}

stdout: binary frames, each introduced by an ASCII header line.
    READY {json}\\n            once, when the model is loaded
    BEGIN <id>\\n
    AUDIO <nbytes>\\n<nbytes raw bytes>      (repeated, one per sentence)
    END <id> <ok|cancelled|error> <detail>\\n

Every ``say`` produces exactly one BEGIN and exactly one END, cancelled or not.
That invariant is what lets the parent drain a cancelled utterance and know
precisely where the next one starts, rather than guessing at byte boundaries.

Diagnostics go to stderr, which the parent logs. stdout is audio only.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
from pathlib import Path

_out = sys.stdout.buffer


def _emit(header: str, payload: bytes = b"") -> None:
    _out.write(header.encode("utf-8"))
    if payload:
        _out.write(payload)
    _out.flush()


def _note(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _note("usage: piper_worker.py <model.onnx> [config.onnx.json]")
        return 2
    model_path = Path(argv[1])
    config_path = Path(argv[2]) if len(argv) > 2 else None

    try:
        from piper import PiperVoice  # noqa: PLC0415 - deliberately lazy
    except Exception as exc:  # noqa: BLE001
        _note(f"piper is not importable in this interpreter: {exc}")
        return 3

    try:
        voice = PiperVoice.load(str(model_path),
                                str(config_path) if config_path else None)
    except Exception as exc:  # noqa: BLE001
        _note(f"could not load {model_path}: {exc}")
        return 4

    rate = getattr(getattr(voice, "config", None), "sample_rate", None)
    _emit("READY " + json.dumps({"sample_rate": rate,
                                 "model": str(model_path)}) + "\n")

    jobs: queue.Queue = queue.Queue()
    state = threading.Lock()
    # A sequence number rather than a flag. A bare Event set while nothing is
    # in flight would silently cancel the *next* utterance instead of the one
    # the user interrupted, which is exactly the barge-in bug worth avoiding.
    counter = {"seq": 0, "cancel_upto": 0}

    def is_cancelled(job_seq: int) -> bool:
        with state:
            return counter["cancel_upto"] >= job_seq

    def synth_loop() -> None:
        while True:
            job = jobs.get()
            if job is None:
                return
            job_id = str(job.get("id") or "")
            job_seq = int(job.get("_seq") or 0)
            sentences = [s for s in job.get("sentences") or [] if s.strip()]
            _emit(f"BEGIN {job_id}\n")
            status, detail = "ok", "-"
            try:
                for sentence in sentences:
                    if is_cancelled(job_seq):
                        status = "cancelled"
                        break
                    for chunk in voice.synthesize(sentence):
                        if is_cancelled(job_seq):
                            status = "cancelled"
                            break
                        data = chunk.audio_int16_bytes
                        if data:
                            _emit(f"AUDIO {len(data)}\n", data)
                    if status == "cancelled":
                        break
            except Exception as exc:  # noqa: BLE001 - a bad sentence must not
                # kill the worker; the parent would otherwise pay a reload.
                status = "error"
                detail = f"{type(exc).__name__}:{exc}".replace("\n", " ")[:200]
                _note(traceback.format_exc())
            _emit(f"END {job_id} {status} {detail}\n")

    worker = threading.Thread(target=synth_loop, daemon=True)
    worker.start()

    # readline(), not `for line in sys.stdin`: iteration over a TextIOWrapper
    # reads ahead into its buffer, which on a pipe can hold a request hostage
    # until more input arrives. A say that never starts is indistinguishable
    # from a hung synthesiser.
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _note(f"ignoring unparseable request: {exc}")
            continue
        op = req.get("op")
        if op == "say":
            with state:
                counter["seq"] += 1
                req["_seq"] = counter["seq"]
            jobs.put(req)
        elif op == "cancel":
            # Cancels everything submitted up to now, in flight or queued, and
            # nothing submitted after. Queued jobs still get BEGIN/END so the
            # parent's frame accounting stays exact.
            with state:
                counter["cancel_upto"] = counter["seq"]
        elif op == "quit":
            jobs.put(None)
            break
        else:
            _note(f"unknown op {op!r}")

    worker.join(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
