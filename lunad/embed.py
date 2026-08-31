"""Semantic recall: one small ONNX sentence embedder, kept off the answer path.

Why this file exists
--------------------
Tier 2 is SQLite + FTS5, and FTS5 cannot cross a vocabulary gap. Measured on
the real database: "how much charge is left" retrieved 0 of 19 episodes while
two of them were about the battery, because "charge" and "battery" share no
token and no stemmer relates them. Dictated input paraphrases constantly, so
that is the common case rather than an edge one. Embeddings fix exactly that
and nothing else -- FTS5 stays, and stays first.

Shape
-----
The same two-role shape as ``lunad/piper_worker.py``, for the same reason:
onnxruntime and numpy live only in ``~/Work/luna/.venv``; lunad itself is stock
system python and is going to stay that way. So this module is

* **imported** by the daemon, where it is pure stdlib and manages a
  subprocess (:class:`Embedder`, and the ``fetch``/``status``/``backfill``
  command line at the bottom); and
* **executed as a script** by ``config.VENV_PYTHON``, where it is the worker
  that holds the model (:func:`_worker_main`).

The package imports below are therefore guarded: run as a script there is no
package, the relative import raises, and the worker half runs without it.

The four rules this file is built around
----------------------------------------
1. **Nothing may slow down an answer.** ``search`` never waits for a cold
   model. If the worker is not up it kicks an asynchronous warm-up and returns
   ``None`` immediately, which means "no semantic opinion" and leaves the
   caller on FTS5 alone. Once warm, every request carries a hard timeout
   (:data:`SEARCH_TIMEOUT_S`), and no exception from here may reach the answer
   path -- every public method swallows and degrades.
2. **No new pip dependency.** The tokenizer is WordPiece in pure Python over
   the model's own ``vocab.txt`` (:class:`WordPiece`). ``tokenizers`` is not
   installed and is not going to be.
3. **Lazy, and unloaded when idle.** Nothing is spawned until the first
   semantic query; the worker is killed after :data:`IDLE_UNLOAD_SECONDS`,
   the same policy speech follows.
4. **Absent by default.** A fresh clone has no model and must still work:
   :meth:`Embedder.available` is false, semantic recall is silently off, and
   FTS5 answers alone. The model arrives only when someone runs
   ``python3 -m lunad.embed fetch``. Nothing downloads itself behind an ask.

Model
-----
``sentence-transformers/all-MiniLM-L6-v2``, Apache-2.0, 384 dimensions, mean
pooling, 86 MB of fp32 ONNX. Credited in the README alongside Hermes and
VoiceMem. Its ``vocab.txt`` is a plain uncased BERT vocabulary, which is what
makes rule 2 affordable.

Worker wire format
------------------
stdin: one JSON object per line, some followed by a raw binary payload.

    {"op": "load", "space": "<key>", "ids": [...], "bytes": N}\\n<N bytes>
    {"op": "search", "id": "<str>", "space": "<key>", "query": "...", "k": 24}
    {"op": "embed", "id": "<str>", "texts": ["...", ...]}
    {"op": "drop", "space": "<key>"}
    {"op": "quit"}

stdout: an ASCII header line, sometimes followed by raw bytes.

    READY {json}\\n
    LOADED <space> <count>\\n
    HITS <id> <json>\\n
    VECS <id> <n> <dim> <nbytes>\\n<nbytes>
    ERR <id> <detail>\\n

Vectors are float32, L2-normalised at the point of creation, so a dot product
*is* the cosine and neither side has to remember to divide.
"""

from __future__ import annotations

import array
import hashlib
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # absent when this file is run as a script by the venv interpreter
    from . import config as _config
    from . import safety as _safety
    from . import settings as _settings_mod
except ImportError:  # pragma: no cover - the worker half never has a package
    _config = None
    _safety = None
    _settings_mod = None


# =========================================================================
# Constants
#
# These live here rather than in config.py deliberately: semantic recall is
# one self-contained feature and every knob it owns should be readable in one
# place. Nothing outside this module needs them.
# =========================================================================

#: Hugging Face repo, and the licence that comes with it. Apache-2.0 is
#: compatible with this project's MIT licence; the obligation it carries is
#: attribution, which the README and ARCHITECTURE.md both honour.
MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_LICENCE = "Apache-2.0"
MODEL_NAME = "all-MiniLM-L6-v2"

#: Files ``fetch`` downloads, with the sha256 each must have. Pinned, because
#: a silently different model is a silently different index: vectors written
#: by one model are meaningless to another, and the failure would look like
#: recall quietly getting worse rather than like anything breaking.
MODEL_FILES: tuple[tuple[str, str, str], ...] = (
    ("onnx/model.onnx", "model.onnx",
     "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
    ("vocab.txt", "vocab.txt",
     "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"),
)

_HF_BASE = f"https://huggingface.co/{MODEL_REPO}/resolve/main/"

#: Output width of the model. Stored per row as well, so a future model swap
#: is detectable rather than silently mixing widths.
EMBED_DIM = 384

#: ``sentence_bert_config.json`` says 256, and going past what the model was
#: trained to pool over buys nothing. Counted including [CLS] and [SEP].
MAX_SEQ_LEN = 256

#: How long the worker survives with nothing to do. Matches the piper
#: worker's policy and ARCHITECTURE.md section 5: ~90 MB of model plus its
#: runtime is worth holding while a conversation is live and not otherwise.
IDLE_UNLOAD_SECONDS = 300.0

#: Hard ceiling on a search once the worker is warm. Generous enough for a
#: 384-dim forward pass over a short query (measured ~10 ms) and short enough
#: that a wedged worker costs a quarter of a second, once, before the latch
#: below turns it off.
SEARCH_TIMEOUT_S = 0.25

#: Backfill runs on a background thread, so it can afford to wait.
EMBED_TIMEOUT_S = 60.0

#: Cold start: python + onnxruntime + a 86 MB graph. Never paid on the ask
#: path -- only a background warm-up thread ever waits this long.
SPAWN_TIMEOUT_S = 30.0

#: How many nearest neighbours the worker returns per query. Four times the
#: usual recall limit, so the coverage gate downstream has something to
#: refuse rather than being handed a pre-trimmed list.
SEARCH_CANDIDATES = 24

#: The cosine-to-coverage calibration. All three numbers are measured.
#:
#: ``recall_block`` refuses anything below ``RECALL_COVERAGE_FLOOR`` (0.5),
#: where coverage means "how much of what you asked about is actually in this
#: episode". The lexical side measures that as a token ratio. The semantic
#: side has to land on the *same* 0-1 line without the floor being moved, so
#: cosine is mapped piecewise through two anchor points:
#:
#:   cos <= FLOOR                 -> 0.0   (not a neighbour at all)
#:   cos == HALF                  -> 0.5   (exactly at the floor)
#:   cos >= FULL                  -> 1.0
#:
#: Piecewise and not one straight line, because a single linear map cannot
#: both cross 0.5 where the data says it should and still saturate somewhere
#: that leaves cosine ordering the strong hits. Two segments do both.
#:
#: HALF is the load-bearing one and it sits in a gap that was measured, not
#: chosen. Over 13 labelled queries against the real 19-episode database
#: (8 with known-relevant episodes, 5 contentful-but-unrelated controls), the
#: lowest true positive scored **0.311** and the highest false positive
#: **0.269** — that FP being "what is on my screen right now?" answering
#: "what terminal do I use", which is a near-miss rather than nonsense. HALF
#: is 0.29, in the middle of that gap. The two battery episodes the whole
#: exercise exists for score 0.419 and 0.324 against "how much charge is
#: left", giving coverage 0.66 and 0.54 — admitted on their own merits, with
#: the floor left exactly where the previous pass put it.
#:
#: The gap is narrow because the corpus is small. Worth re-measuring, with
#: the same script, once tier 2 holds thousands of episodes rather than
#: nineteen.
SEMANTIC_FLOOR = 0.15
SEMANTIC_HALF = 0.29
SEMANTIC_FULL = 0.70

#: How much of the user's turn goes into the embedded text.
#:
#: **Only the user's turn.** Not the exchange, and this was measured both
#: ways. Embedding ``user + luna`` recovers exactly one case out of thirteen
#: (a question whose answer, not whose question, holds the subject) and costs
#: a false positive of the same magnitude — "hello" scoring 0.320 against
#: "what terminal do I use", because Luna's replies share a great deal of
#: boilerplate with each other and almost none of it is about anything. That
#: is buying recall with precision at 1:1, which is the wrong trade for text
#: that gets injected into a prompt as confirmed context.
#:
#: It is also the wrong *division of labour*. FTS5 already indexes both
#: sides of every exchange, so the answer text is covered lexically. Making
#: the vector cover the user's phrasing instead gives the union two halves
#: that fail differently: keywords in either speaker's words, paraphrase in
#: the asker's. A union is only worth having when its sources disagree.
#:
#: The model pools 256 word pieces, so past roughly a thousand characters
#: the tokenizer drops the rest anyway; clipping here makes that explicit.
EMBED_USER_CHARS = 900

#: Episodes embedded per background batch.
#:
#: Four, and small on purpose. Batching buys nothing here and costs memory
#: linearly: measured on 256-token episodes, one at a time is 102 ms each at a
#: 198 MB peak, four at a time is 121 ms each at 225 MB, thirty-two at a time
#: is 126 ms each at **573 MB**. The attention tensors scale with batch times
#: sequence squared and the worker is single-threaded by design, so a wide
#: batch is a memory spike bought with nothing. Four keeps the peak inside
#: what a 7.1 GB machine can spare while still committing only once per four
#: episodes -- and a batch is the unit of work a kill can cost.
BACKFILL_BATCH = 4

#: Seconds the backfill thread sleeps between batches. Backfill is never
#: urgent and must never compete with an answer for the same cores.
BACKFILL_PAUSE_S = 0.2

#: Outstanding-episode count above which a backfill waits for mains power.
#:
#: Measured on this machine: 28 ms of one core per episode over the real
#: corpus, 102 ms for one long enough to fill the model's 256-token window.
#: Catching up the
#: handful of episodes recorded since the last session is 1-2 seconds and not
#: worth a policy. A first-ever index over thousands of episodes is minutes
#: of sustained full-core work, which on a Ryzen 4500U is a visible chunk of
#: a battery charge for a result nobody is waiting on. So: small catch-ups
#: run anywhere, a big first pass waits until the laptop is plugged in, and
#: ``python3 -m lunad.embed backfill --force`` overrides that for someone who
#: knows they are about to close the lid.
BACKFILL_BATTERY_LIMIT = 64


# =========================================================================
# Paths
# =========================================================================


def models_dir() -> Path:
    """Where downloaded models live. User state, never the repo.

    Read late, from ``config`` when there is one, so a test that redirects
    ``config.STATE_DIR`` is obeyed rather than bypassed by an import-time
    constant. Falls back to the XDG default for the worker half, which has no
    package to import.
    """
    if _config is not None:
        return _config.STATE_DIR / "models"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "luna" / "models"


def model_dir() -> Path:
    return models_dir() / MODEL_NAME


def model_present(directory: Path | None = None) -> bool:
    """True when every file :func:`fetch` writes is on disk and non-empty."""
    directory = directory or model_dir()
    for _remote, local, _digest in MODEL_FILES:
        path = directory / local
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


def compose_episode_text(user_text: str, luna_text: str = "") -> str:
    """The text an episode is embedded as. One place, so writes and queries agree.

    ``luna_text`` is accepted and deliberately ignored — see
    :data:`EMBED_USER_CHARS` for the measurement behind that. It stays in the
    signature so callers pass the whole episode and the decision lives here,
    rather than being re-made at every call site.
    """
    return (user_text or "").strip()[:EMBED_USER_CHARS]


def coverage_from_cosine(cos: float) -> float:
    """Map a cosine onto the same 0-1 line the lexical coverage lives on.

    See :data:`SEMANTIC_HALF`. Clamped at both ends: at or below the floor a
    hit contributes nothing at all, rather than a small positive number that
    could accumulate into a false sense of relevance.
    """
    if cos <= SEMANTIC_FLOOR:
        return 0.0
    if cos >= SEMANTIC_FULL:
        return 1.0
    if cos <= SEMANTIC_HALF:
        return 0.5 * (cos - SEMANTIC_FLOOR) / (SEMANTIC_HALF - SEMANTIC_FLOOR)
    return 0.5 + 0.5 * (cos - SEMANTIC_HALF) / (SEMANTIC_FULL - SEMANTIC_HALF)


# =========================================================================
# WordPiece — the whole tokenizer, in stdlib
# =========================================================================
#
# This is `BertTokenizer` with the settings this model's tokenizer_config.json
# actually declares: do_lower_case=true, tokenize_chinese_chars=true,
# strip_accents=null (which BERT resolves to "follow do_lower_case", so
# accents are stripped). It is deterministic and about a hundred lines, which
# is a far better trade than adding `tokenizers` — a Rust wheel — to a project
# whose whole point is that the daemon is stdlib.

_CONTROL_CATEGORIES = ("Cc", "Cf")


def _is_whitespace(ch: str) -> bool:
    if ch in " \t\n\r":
        return True
    return unicodedata.category(ch) == "Zs"


def _is_control(ch: str) -> bool:
    if ch in "\t\n\r":
        return False
    return unicodedata.category(ch) in _CONTROL_CATEGORIES


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    # BERT treats the ASCII non-alphanumeric ranges as punctuation even where
    # Unicode disagrees (e.g. "$", "+", "^"), so this mirrors that exactly
    # rather than being tidier and wrong.
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(cp: int) -> bool:
    return (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2B73F
        or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF
        or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F
    )


class WordPiece:
    """The model's own vocabulary, greedily longest-match-first."""

    UNK = "[UNK]"
    CLS = "[CLS]"
    SEP = "[SEP]"
    PAD = "[PAD]"
    MAX_CHARS_PER_WORD = 100

    def __init__(self, vocab: dict[str, int]) -> None:
        self.vocab = vocab
        self.unk_id = vocab[self.UNK]
        self.cls_id = vocab[self.CLS]
        self.sep_id = vocab[self.SEP]
        self.pad_id = vocab[self.PAD]

    @classmethod
    def load(cls, path: Path) -> "WordPiece":
        vocab: dict[str, int] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                vocab[line.rstrip("\n")] = index
        for required in (cls.UNK, cls.CLS, cls.SEP, cls.PAD):
            if required not in vocab:
                raise ValueError(f"{path} is not a BERT vocabulary: no {required}")
        return cls(vocab)

    # -- basic tokenizer -------------------------------------------------

    def _clean(self, text: str) -> str:
        out = []
        for ch in text:
            cp = ord(ch)
            if cp == 0 or cp == 0xFFFD or _is_control(ch):
                continue
            if _is_whitespace(ch):
                out.append(" ")
            elif _is_cjk(cp):
                # Every CJK character is its own token, so it is padded here
                # and the whitespace split below does the rest.
                out.append(f" {ch} ")
            else:
                out.append(ch)
        return "".join(out)

    def _strip_accents(self, text: str) -> str:
        return "".join(ch for ch in unicodedata.normalize("NFD", text)
                       if unicodedata.category(ch) != "Mn")

    def _split_punctuation(self, token: str) -> list[str]:
        pieces: list[str] = []
        current: list[str] = []
        for ch in token:
            if _is_punctuation(ch):
                if current:
                    pieces.append("".join(current))
                    current = []
                pieces.append(ch)
            else:
                current.append(ch)
        if current:
            pieces.append("".join(current))
        return pieces

    def basic_tokens(self, text: str) -> list[str]:
        out: list[str] = []
        for word in self._clean(text).split():
            word = self._strip_accents(word.lower())
            out.extend(p for p in self._split_punctuation(word) if p)
        return out

    # -- wordpiece -------------------------------------------------------

    def wordpiece(self, token: str) -> list[str]:
        if len(token) > self.MAX_CHARS_PER_WORD:
            return [self.UNK]
        pieces: list[str] = []
        start = 0
        while start < len(token):
            end = len(token)
            found: str | None = None
            while start < end:
                candidate = token[start:end]
                if start > 0:
                    candidate = "##" + candidate
                if candidate in self.vocab:
                    found = candidate
                    break
                end -= 1
            if found is None:
                return [self.UNK]
            pieces.append(found)
            start = end
        return pieces

    def encode(self, text: str, max_len: int = MAX_SEQ_LEN) -> list[int]:
        """``[CLS] … [SEP]`` ids, truncated to ``max_len`` including both."""
        ids = [self.cls_id]
        budget = max_len - 2
        for token in self.basic_tokens(text):
            if len(ids) - 1 >= budget:
                break
            for piece in self.wordpiece(token):
                if len(ids) - 1 >= budget:
                    break
                ids.append(self.vocab.get(piece, self.unk_id))
        ids.append(self.sep_id)
        return ids


# =========================================================================
# The worker — runs under the venv interpreter, holds the model
# =========================================================================


def _wnote(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _worker_main(argv: list[str]) -> int:
    """``VENV_PYTHON lunad/embed.py worker <model-dir>``.

    Single-threaded on purpose. Unlike speech there is nothing to cancel and
    no barge-in to get right: every request is milliseconds and the parent
    times out on its own side, so a request queue would be machinery with no
    behaviour attached to it.
    """
    if len(argv) < 3:
        _wnote("usage: embed.py worker <model-dir>")
        return 2
    directory = Path(argv[2])

    try:
        import numpy as np  # noqa: PLC0415 - deliberately lazy
        import onnxruntime as ort  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        _wnote(f"this interpreter has no onnxruntime/numpy: {exc}")
        return 3

    try:
        tok = WordPiece.load(directory / "vocab.txt")
        options = ort.SessionOptions()
        # One thread. The daemon is answering a question on the other cores
        # and a 384-dim forward pass over one short sentence does not need
        # more; measured, four threads made it slower, not faster.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # The CPU arena allocator never returns memory to the OS, so a single
        # wide backfill batch permanently sets the worker's resident size --
        # measured at 486 MB with the arena on against 7.1 GB of machine.
        # Off, it holds close to the graph itself and gives the pages back.
        options.enable_cpu_mem_arena = False
        session = ort.InferenceSession(
            str(directory / "model.onnx"), options,
            providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001
        _wnote(f"could not load the embedding model from {directory}: {exc}")
        return 4

    input_names = {i.name for i in session.get_inputs()}
    out = sys.stdout.buffer
    stdin = sys.stdin.buffer

    def emit(header: str, payload: bytes = b"") -> None:
        out.write(header.encode("utf-8"))
        if payload:
            out.write(payload)
        out.flush()

    def embed(texts: Sequence[str]) -> "np.ndarray":
        rows = [tok.encode(t) for t in texts]
        width = max(len(r) for r in rows)
        ids = np.zeros((len(rows), width), dtype=np.int64)
        mask = np.zeros((len(rows), width), dtype=np.int64)
        for i, row in enumerate(rows):
            ids[i, :len(row)] = row
            mask[i, :len(row)] = 1
        feed: dict[str, Any] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in input_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = session.run(None, feed)[0]
        # Mean pooling over real tokens only, then L2 normalise, which is
        # what sentence-transformers does for this model and the only reason
        # a plain dot product downstream is a cosine.
        m = mask.astype(np.float32)[..., None]
        pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-9, None)).astype(np.float32)

    spaces: dict[str, dict[str, Any]] = {}

    # Emitted only after the graph is loaded AND one warm-up forward pass has
    # run. onnxruntime defers a good deal of allocation to the first call, so
    # a READY sent before it would hand the parent a worker whose first real
    # query costs 200 ms instead of 10 -- and that first query is on the ask
    # path, which is the one place the budget is not there to spend.
    try:
        embed(["warm up"])
    except Exception as exc:  # noqa: BLE001
        _wnote(f"the model loaded but would not run: {exc}")
        return 5
    _emit_ready = json.dumps({"model": str(directory), "dim": EMBED_DIM,
                              "max_seq": MAX_SEQ_LEN})
    emit(f"READY {_emit_ready}\n")

    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            _wnote(f"ignoring unparseable request: {exc}")
            continue
        op = req.get("op")
        req_id = str(req.get("id") or "-")
        try:
            if op == "quit":
                break
            if op == "load":
                nbytes = int(req.get("bytes") or 0)
                blob = stdin.read(nbytes) if nbytes else b""
                ids = [int(i) for i in req.get("ids") or []]
                space = str(req.get("space") or "")
                mat = np.frombuffer(blob, dtype=np.float32)
                mat = mat.reshape(len(ids), -1) if ids else mat.reshape(0, EMBED_DIM)
                slot = spaces.setdefault(space, {"ids": [], "mat": None})
                slot["ids"] = list(slot["ids"]) + ids
                slot["mat"] = mat if slot["mat"] is None else np.vstack(
                    [slot["mat"], mat])
                emit(f"LOADED {space} {len(slot['ids'])}\n")
            elif op == "drop":
                spaces.pop(str(req.get("space") or ""), None)
            elif op == "search":
                space = str(req.get("space") or "")
                slot = spaces.get(space)
                if not slot or slot["mat"] is None or not len(slot["ids"]):
                    emit(f"HITS {req_id} []\n")
                    continue
                q = embed([str(req.get("query") or "")])[0]
                scores = slot["mat"] @ q
                k = min(int(req.get("k") or SEARCH_CANDIDATES), len(scores))
                top = np.argpartition(-scores, k - 1)[:k] if k < len(scores) \
                    else np.arange(len(scores))
                pairs = sorted(((int(slot["ids"][i]), float(scores[i]))
                                for i in top), key=lambda p: -p[1])
                emit(f"HITS {req_id} {json.dumps(pairs, separators=(',', ':'))}\n")
            elif op == "embed":
                texts = [str(t) for t in req.get("texts") or []]
                if not texts:
                    emit(f"VECS {req_id} 0 {EMBED_DIM} 0\n")
                    continue
                vecs = embed(texts)
                payload = vecs.tobytes()
                emit(f"VECS {req_id} {vecs.shape[0]} {vecs.shape[1]} "
                     f"{len(payload)}\n", payload)
            else:
                emit(f"ERR {req_id} unknown-op:{op}\n")
        except Exception as exc:  # noqa: BLE001 - one bad request must not
            # cost a reload; the parent would pay a cold start for it.
            detail = f"{type(exc).__name__}:{exc}".replace("\n", " ")[:200]
            emit(f"ERR {req_id} {detail}\n")
    return 0


# =========================================================================
# The parent side — what the daemon imports
# =========================================================================


def _log() -> Any:
    import logging  # noqa: PLC0415 - keeps the worker half import-free
    return logging.getLogger("lunad.embed")


class Embedder:
    """Owns the worker process. Every method degrades rather than raises.

    The contract the answer path depends on: :meth:`search` either returns a
    mapping of episode id to cosine, or ``None`` meaning "no opinion". It
    never blocks on a cold model, never propagates an exception, and never
    takes longer than :data:`SEARCH_TIMEOUT_S` once warm.
    """

    def __init__(self, directory: Path | None = None,
                 python: Path | None = None,
                 idle_seconds: float = IDLE_UNLOAD_SECONDS) -> None:
        self._dir = directory
        self._python = python
        self._idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._pending: dict[str, queue.Queue] = {}
        self._ready = threading.Event()
        self._starting = False
        self._seq = 0
        self._last_use = 0.0
        self._timer: threading.Timer | None = None
        self._closed = False
        #: Latched after a failed spawn. A missing model or a broken venv is
        #: not a transient condition, and retrying it once per question would
        #: fork a doomed process on every ask for the life of the daemon.
        self._broken = ""
        #: space -> highest episode id this worker has been told about.
        self._watermarks: dict[str, int] = {}

    # -- availability ----------------------------------------------------

    def directory(self) -> Path:
        return self._dir if self._dir is not None else model_dir()

    def python(self) -> Path:
        if self._python is not None:
            return self._python
        # Read late. `tests/_support.py` replaces config.VENV_PYTHON
        # process-wide with a path that cannot resolve, which is what keeps
        # the whole suite from ever forking a real model; binding it at
        # import would defeat that.
        return _config.VENV_PYTHON if _config is not None else Path(sys.executable)

    def available(self) -> bool:
        """Model on disk and an interpreter that could load it. No side effects."""
        if self._broken or self._closed:
            return False
        try:
            return model_present(self.directory()) and self.python().exists()
        except OSError:
            return False

    def enabled(self) -> bool:
        """:data:`available` and the user has not turned it off."""
        if not self.available():
            return False
        if _settings_mod is None:
            return True
        value = _settings_mod.get("memory.semantic_recall")
        return True if value is None else bool(value)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    # -- lifecycle -------------------------------------------------------

    def warm(self) -> None:
        """Start the worker in the background. Returns immediately, always."""
        with self._lock:
            if self._closed or self._ready.is_set() or self._starting:
                return
            if not self.enabled():
                return
            self._starting = True
        threading.Thread(target=self._spawn, name="luna-embed-warm",
                         daemon=True).start()

    def wait_ready(self, timeout: float = SPAWN_TIMEOUT_S) -> bool:
        """Block until warm. Only ever called off the answer path.

        Returns ``False`` at once — not after ``timeout`` — when nothing is
        starting and nothing can. Waiting on an event no one is going to set
        is how a machine with no model made every background backfill sit
        still for thirty seconds before concluding what ``enabled()`` already
        knew.
        """
        self.warm()
        with self._lock:
            if not (self._starting or self._ready.is_set()
                    or self._proc is not None):
                return False
        return self._ready.wait(timeout)

    def _spawn(self) -> None:
        if _safety is None:  # pragma: no cover - worker-mode import guard
            self._fail("no process firewall available")
            return
        try:
            # Through the firewall, never a bare Popen: a pid with no ledger
            # record is a pid nothing is allowed to stop, and
            # `tests/test_safety.py` reads this file to make sure it stays
            # that way. `spawn` supplies start_new_session itself.
            proc = _safety.spawn(
                [str(self.python()), str(Path(__file__).resolve()),
                 "worker", str(self.directory())],
                kind="embed", durable=False,
                note="embedding worker for semantic recall",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"could not start the embedding worker: {exc}")
            return
        with self._lock:
            self._proc = proc
            self._reader = threading.Thread(
                target=self._read_loop, args=(proc,),
                name="luna-embed-reader", daemon=True)
            self._reader.start()
            threading.Thread(target=self._drain_stderr, args=(proc,),
                             name="luna-embed-stderr", daemon=True).start()
        if not self._ready.wait(SPAWN_TIMEOUT_S):
            self._fail("the embedding worker did not become ready")
            return
        with self._lock:
            self._starting = False
        self._touch()

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw in stream:
            try:
                _log().warning("embed worker: %s",
                               raw.decode("utf-8", "replace").rstrip())
            except Exception:  # noqa: BLE001
                return

    def _read_loop(self, proc: subprocess.Popen[bytes]) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            while True:
                header = stream.readline()
                if not header:
                    break
                # Parsed field by field rather than with one `split(" ", n)`:
                # a HITS body is JSON and JSON contains spaces, while a VECS
                # header has a fixed five fields. One maxsplit cannot be right
                # for both, and getting it wrong on VECS leaves the binary
                # payload unread in the pipe, which desynchronises every frame
                # after it. That bug cost an afternoon; hence this comment.
                kind, _, rest = header.decode("utf-8", "replace") \
                    .rstrip("\n").partition(" ")
                if kind == "READY":
                    self._ready.set()
                elif kind == "HITS":
                    req_id, _, body = rest.partition(" ")
                    self._deliver(req_id, ("hits", body))
                elif kind == "VECS":
                    req_id, n, dim, nbytes = rest.split(" ", 3)
                    payload = stream.read(int(nbytes)) if int(nbytes) else b""
                    self._deliver(req_id, ("vecs", (int(n), int(dim), payload)))
                elif kind == "ERR":
                    req_id, _, detail = rest.partition(" ")
                    self._deliver(req_id, ("err", detail))
                elif kind == "LOADED":
                    pass
        except Exception as exc:  # noqa: BLE001
            _log().warning("embed reader stopped: %s", exc)
        finally:
            self._on_exit()

    def _deliver(self, req_id: str, payload: tuple[str, Any]) -> None:
        with self._lock:
            waiter = self._pending.pop(req_id, None)
        # No waiter means the request already timed out. Dropping the reply is
        # correct and is why every request carries an id: a late answer can
        # never be mistaken for the next question's.
        if waiter is not None:
            waiter.put(payload)

    def _on_exit(self) -> None:
        with self._lock:
            self._ready.clear()
            self._starting = False
            self._proc = None
            self._watermarks.clear()
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter.put(("err", "worker exited"))

    def _fail(self, reason: str) -> None:
        _log().warning("semantic recall is off: %s", reason)
        with self._lock:
            self._broken = reason
            self._starting = False
        self._kill()

    def _kill(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready.clear()
            self._watermarks.clear()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if proc is None:
            return
        # Ask first: a worker that gets `quit` exits on its own and never
        # needs a signal at all, which is the common case (idle unload).
        if proc.stdin is not None:
            for step in (lambda: proc.stdin.write(b'{"op":"quit"}\n'),
                         proc.stdin.flush, proc.stdin.close):
                try:
                    step()
                except (OSError, ValueError):
                    break
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            # Then through the firewall, and only through the firewall.
            try:
                if _safety is not None:
                    _safety.terminate(proc, grace=2.0,
                                      reason="embedding model unloaded")
            except Exception as exc:  # noqa: BLE001
                _log().warning("could not stop the embedding worker: %s", exc)
        except Exception as exc:  # noqa: BLE001
            _log().warning("could not stop the embedding worker: %s", exc)
        finally:
            if _safety is not None:
                _safety.reap(proc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._kill()

    # -- idle unload -----------------------------------------------------

    def _touch(self) -> None:
        with self._lock:
            self._last_use = time.monotonic()
            if self._timer is not None:
                self._timer.cancel()
            if self._closed or self._idle_seconds <= 0:
                self._timer = None
                return
            self._timer = threading.Timer(self._idle_seconds, self._idle_check)
            self._timer.daemon = True
            self._timer.start()

    def _idle_check(self) -> None:
        with self._lock:
            idle = time.monotonic() - self._last_use
            if idle < self._idle_seconds - 0.01:
                return
        _log().info("unloading the embedding model after %.0f s idle",
                    self._idle_seconds)
        self._kill()

    # -- requests --------------------------------------------------------

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return str(self._seq)

    def _request(self, payload: dict[str, Any], timeout: float,
                 blob: bytes = b"", expect_reply: bool = True) -> Any:
        proc = self._proc
        if proc is None or proc.stdin is None or not self._ready.is_set():
            return None
        req_id = payload.get("id")
        waiter: queue.Queue | None = None
        if expect_reply and req_id is not None:
            waiter = queue.Queue(maxsize=1)
            with self._lock:
                self._pending[str(req_id)] = waiter
        try:
            line = (json.dumps(payload) + "\n").encode("utf-8")
            with self._lock:
                proc.stdin.write(line)
                if blob:
                    proc.stdin.write(blob)
                proc.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            if waiter is not None:
                with self._lock:
                    self._pending.pop(str(req_id), None)
            _log().warning("embedding request failed to send: %s", exc)
            return None
        self._touch()
        if waiter is None:
            return None
        try:
            kind, value = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(str(req_id), None)
            _log().warning("embedding request timed out after %.2fs", timeout)
            return None
        if kind == "err":
            _log().warning("embedding worker error: %s", value)
            return None
        return value

    # -- public surface --------------------------------------------------

    def sync(self, space: str, rows: Sequence[tuple[int, bytes]]) -> int:
        """Hand the worker vectors it does not have. Returns how many landed."""
        rows = [(int(i), b) for i, b in rows if b]
        if not rows or not self._ready.is_set():
            return 0
        ids = [i for i, _ in rows]
        blob = b"".join(b for _, b in rows)
        self._request({"op": "load", "space": space, "ids": ids,
                       "bytes": len(blob)}, timeout=0.0, blob=blob,
                      expect_reply=False)
        with self._lock:
            current = self._watermarks.get(space, 0)
            self._watermarks[space] = max(current, max(ids))
        return len(rows)

    def watermark(self, space: str) -> int | None:
        """Highest episode id the worker knows about, or ``None`` if not warm."""
        if not self._ready.is_set():
            return None
        with self._lock:
            return self._watermarks.get(space, 0)

    def search(self, space: str, query: str,
               k: int = SEARCH_CANDIDATES) -> dict[int, float] | None:
        """Nearest neighbours, or ``None`` for "no semantic opinion".

        Never waits for a cold model: a miss kicks the warm-up and returns
        immediately, so the very first question after a restart is answered on
        FTS5 alone rather than a second late.
        """
        try:
            if self._closed or not query.strip():
                return None
            if not self._ready.is_set():
                self.warm()
                return None
            result = self._request(
                {"op": "search", "id": self._next_id(), "space": space,
                 "query": query, "k": int(k)}, timeout=SEARCH_TIMEOUT_S)
            if result is None:
                return None
            pairs = json.loads(result)
            return {int(i): float(s) for i, s in pairs}
        except Exception as exc:  # noqa: BLE001 - the answer path ends here
            _log().warning("semantic search failed, falling back to FTS: %s", exc)
            return None

    def embed(self, texts: Sequence[str],
              timeout: float = EMBED_TIMEOUT_S) -> list[bytes] | None:
        """Vectors for ``texts`` as raw float32 blobs. Backfill only, may block."""
        try:
            if self._closed or not texts:
                return None
            if not self._ready.is_set() and not self.wait_ready():
                return None
            result = self._request(
                {"op": "embed", "id": self._next_id(),
                 "texts": list(texts)}, timeout=timeout)
            if result is None:
                return None
            n, dim, payload = result
            if n != len(texts) or dim <= 0 or len(payload) != n * dim * 4:
                _log().warning("embedding worker returned a malformed batch")
                return None
            width = dim * 4
            return [payload[i * width:(i + 1) * width] for i in range(n)]
        except Exception as exc:  # noqa: BLE001
            _log().warning("embedding failed: %s", exc)
            return None

    def status(self) -> dict[str, Any]:
        return {
            "model": MODEL_NAME,
            "licence": MODEL_LICENCE,
            "dir": str(self.directory()),
            "present": model_present(self.directory()),
            "python": str(self.python()),
            "available": self.available(),
            "enabled": self.enabled(),
            "ready": self.ready,
            "broken": self._broken,
            "dim": EMBED_DIM,
        }


# =========================================================================
# The process-wide singleton
# =========================================================================
#
# One worker for the whole daemon, not one per store. Luna and Sol each have
# their own episode database, and two workers would be two copies of the model
# resident against 3-4 GB of headroom for no gain: the model is the same, only
# the vectors differ, so the worker keeps them in separate *spaces* keyed by
# database path instead.

_embedder_lock = threading.Lock()
_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            _embedder = Embedder()
        return _embedder


def use_embedder(replacement: Embedder | None) -> Embedder | None:
    """Swap the singleton and return the old one. Tests do this; nothing else."""
    global _embedder
    with _embedder_lock:
        previous, _embedder = _embedder, replacement
        return previous


# =========================================================================
# Power
# =========================================================================


def on_mains_power() -> bool | None:
    """True on AC, False on battery, ``None`` when the machine will not say.

    ``None`` is treated as mains everywhere it is used: a desktop with no
    battery at all reports nothing, and refusing to index there would be the
    wrong default.
    """
    try:
        base = Path("/sys/class/power_supply")
        if not base.is_dir():
            return None
        for supply in sorted(base.iterdir()):
            try:
                if (supply / "type").read_text().strip() != "Mains":
                    continue
                return (supply / "online").read_text().strip() == "1"
            except OSError:
                continue
    except OSError:
        return None
    return None


# =========================================================================
# Command line: fetch / status / backfill / worker
# =========================================================================


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(directory: Path | None = None, force: bool = False) -> int:
    """Download the model. Explicit, never triggered by a question."""
    from urllib.request import urlopen  # noqa: PLC0415

    directory = directory or model_dir()
    directory.mkdir(parents=True, exist_ok=True)
    print(f"model : {MODEL_REPO} ({MODEL_LICENCE})")
    print(f"into  : {directory}")
    for remote, local, digest in MODEL_FILES:
        target = directory / local
        if target.is_file() and not force and _sha256(target) == digest:
            print(f"  ok    {local} (already present)")
            continue
        tmp = target.with_name(target.name + ".part")
        url = _HF_BASE + remote
        print(f"  get   {local} <- {url}")
        try:
            with urlopen(url, timeout=60) as response, open(tmp, "wb") as fh:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
        except Exception as exc:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            print(f"  FAIL  {local}: {exc}", file=sys.stderr)
            return 1
        got = _sha256(tmp)
        if got != digest:
            tmp.unlink(missing_ok=True)
            print(f"  FAIL  {local}: sha256 {got}, expected {digest}",
                  file=sys.stderr)
            return 1
        # Renamed only after the digest matches, so an interrupted download
        # leaves a .part behind rather than a half model that looks present.
        tmp.replace(target)
        print(f"  ok    {local} ({target.stat().st_size:,} bytes)")
    print("done. Semantic recall is on from the next daemon start.")
    return 0


def _cli_status() -> int:
    emb = get_embedder()
    for key, value in emb.status().items():
        print(f"{key:>10}: {value}")
    power = on_mains_power()
    print(f"{'power':>10}: {'mains' if power in (True, None) else 'battery'}")
    return 0


def _cli_backfill(force: bool) -> int:
    from .memory import EpisodeStore  # noqa: PLC0415

    store = EpisodeStore()
    try:
        done, remaining = store.backfill_vectors(force=force, budget=None)
        print(f"embedded {done}, {remaining} still without a vector")
        return 0
    finally:
        store.close()


def cli(argv: Sequence[str]) -> int:
    args = list(argv[1:])
    command = args[0] if args else "status"
    if command == "worker":
        return _worker_main(list(argv))
    if command == "fetch":
        return fetch(force="--force" in args)
    if command == "status":
        return _cli_status()
    if command == "backfill":
        return _cli_backfill(force="--force" in args)
    print(f"usage: python3 -m lunad.embed [status|fetch|backfill|worker] "
          f"(unknown command {command!r})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv))
