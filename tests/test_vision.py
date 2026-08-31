"""`luna look` — a screenshot reaching the model, and nothing left behind.

`gpt-5.6-luna` has vision natively and `codex exec -i <FILE>` attaches an image
to the turn, so this is an ask with a picture and not a mode of its own: same
session, same memory, same recall, same episode written afterwards. There is no
second model, no separate vision service and no HTTP call anywhere in it —
OpenRouter is for text-to-speech and nothing else.

What is actually worth testing is the two promises around the picture rather
than the picture itself:

* it is taken only when a look was asked for, never ambiently; and
* the file is gone when the call returns, including when the call blew up.

The adapter is stubbed and `grim` is a shell script, so nothing here photographs
the machine running the suite. test_guards.py asserts that it cannot.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from lunad import agent, config, context, dispatch
from lunad.server import Daemon

from ._support import FakeHyprland, TempMemoryCase


class SeeingAdapter(agent.BaseAdapter):
    """An adapter that can be shown an image, and remembers whether it was.

    ``ask_has_tools`` is True because that is what the real codex ask path is
    now, and because the server reads it to decide both which operating notes
    Luna gets and whether a look is possible at all.
    """

    name = "fake-codex"
    ask_has_tools = True
    accepts_images = True

    def __init__(self) -> None:
        self.images: list[list[str]] = []
        self.existed: list[bool] = []
        self.last_prompt = ""

    def available(self) -> tuple[bool, str]:
        return True, "fake seeing adapter"

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> agent.AgentReply:
        images = [str(i) for i in kw.get("images") or ()]
        self.images.append(images)
        # The picture has to be readable *at the moment of the call*, which is
        # the half of "deleted afterwards" that a cleanup test cannot show.
        self.existed.append(all(Path(i).is_file() for i in images))
        self.last_prompt = prompt
        return agent.AgentReply(text="A terminal, mostly.", agent=self.name,
                                model="gpt-5.6-luna", billing="subscription",
                                wall_ms=1)


class BlindAdapter(agent.BaseAdapter):
    """claude's ask path: no tools, and no way to be shown a file."""

    name = "claude"
    ask_has_tools = False

    def available(self) -> tuple[bool, str]:
        return True, "fake blind adapter"

    def ask(self, prompt: str, system_prompt: str, **kw: Any) -> agent.AgentReply:
        return agent.AgentReply(text="I cannot see.", agent="claude", wall_ms=1)


class LookCase(TempMemoryCase):
    def daemon(self, adapter: agent.BaseAdapter | None = None) -> Daemon:
        dispatcher = dispatch.Dispatcher(jobs_dir=self.root / "jobs",
                                         hypr=FakeHyprland(), audit=self.audit,
                                         sol_memory_dir=self.root / "sol",
                                         terminal="/bin/bash",
                                         agent_bin="/bin/true")
        d = Daemon(agent_name="codex", memory=self.memory(),
                   sol_memory=self.sol_memory(), audit=self.audit,
                   dispatcher=dispatcher)
        d.adapter = adapter or SeeingAdapter()
        d.speech.close()
        d.speech = _Mute()
        self.addCleanup(d.close)
        return d

    def grim(self, script: str = 'printf PNG > "$LAST"') -> None:
        path = self.root / "fake-grim"
        path.write_text("#!/bin/sh\nfor LAST; do :; done\n" + script + "\n",
                        encoding="utf-8")
        path.chmod(0o700)
        old = config.GRIM_BIN
        config.GRIM_BIN = str(path)
        self.addCleanup(setattr, config, "GRIM_BIN", old)

    # -- the happy path ---------------------------------------------------

    def test_a_look_hands_the_adapter_a_readable_image(self) -> None:
        self.grim()
        adapter = SeeingAdapter()
        d = self.daemon(adapter)
        resp = d.dispatch({"op": "look", "prompt": "what is this window"})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(len(adapter.images[0]), 1)
        self.assertTrue(adapter.existed[0], "the image was gone before the call")

    def test_the_image_is_deleted_once_the_answer_is_back(self) -> None:
        self.grim()
        adapter = SeeingAdapter()
        d = self.daemon(adapter)
        d.dispatch({"op": "look", "prompt": "what is this"})
        path = Path(adapter.images[0][0])
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_the_image_is_deleted_when_the_agent_falls_over(self) -> None:
        """The case that leaves a photograph of the screen in /tmp forever."""
        self.grim()
        seen: list[str] = []

        class Exploding(SeeingAdapter):
            def ask(self, prompt: str, system_prompt: str,
                    **kw: Any) -> agent.AgentReply:
                seen.extend(str(i) for i in kw.get("images") or ())
                raise agent.AgentFailed("the model fell over", returncode=1)

        d = self.daemon(Exploding())
        resp = d.dispatch({"op": "look", "prompt": "what is this"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "AgentFailed")
        self.assertTrue(seen)
        self.assertFalse(Path(seen[0]).exists())
        self.assertFalse(Path(seen[0]).parent.exists())

    def test_a_look_with_no_question_still_asks_something(self) -> None:
        self.grim()
        adapter = SeeingAdapter()
        d = self.daemon(adapter)
        resp = d.dispatch({"op": "look"})
        self.assertTrue(resp["ok"], resp)
        self.assertIn("screen", adapter.last_prompt.lower())

    def test_a_look_is_remembered_like_any_other_exchange(self) -> None:
        # Same session, same memory: the picture is context for one turn, but
        # what she concluded from it is worth keeping.
        self.grim()
        d = self.daemon()
        resp = d.dispatch({"op": "look", "prompt": "what is this"})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(d.memory.episodes.recent(1)[0].luna_text,
                         "A terminal, mostly.")

    # -- nothing ambient --------------------------------------------------

    def test_an_ordinary_ask_photographs_nothing(self) -> None:
        """The line that matters most in this file.

        A daemon that screenshots on every turn "just in case" is a different
        piece of software from the one the user agreed to run. Capture happens
        if and only if a look was requested.
        """
        self.grim()
        adapter = SeeingAdapter()
        d = self.daemon(adapter)
        with mock.patch.object(context, "capture",
                               side_effect=AssertionError("captured!")):
            resp = d.dispatch({"op": "ask", "prompt": "how are you"})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(adapter.images, [[]])

    # -- failure ----------------------------------------------------------

    def test_a_grim_that_fails_is_an_answer_not_a_crash(self) -> None:
        self.grim("echo 'no wayland display' >&2; exit 1")
        d = self.daemon()
        resp = d.dispatch({"op": "look", "prompt": "what is this"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "LookUnavailable")
        self.assertIn("no wayland display", resp["message"])

    def test_a_blind_agent_says_so_rather_than_guessing_from_the_title(self) -> None:
        """The worst available failure would be a confident description.

        A dropped image and a window title in the context line is everything a
        model needs to narrate a screen nobody looked at.
        """
        d = self.daemon(BlindAdapter())
        d.agent_name = "claude"
        resp = d.dispatch({"op": "look", "prompt": "what is this"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "LookUnavailable")
        self.assertIn("codex", resp["message"])

    def test_an_unknown_scope_is_refused(self) -> None:
        self.grim()
        d = self.daemon()
        resp = d.dispatch({"op": "look", "scope": "everything"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"], "ProtocolError")

    def test_look_is_a_real_op(self) -> None:
        d = self.daemon()
        resp = d.dispatch({"op": "loook"})
        self.assertIn("look", resp["message"])


class ArgvCase(unittest.TestCase):
    """What the image actually looks like on the codex command line."""

    def test_the_flag_is_i_and_it_comes_last(self) -> None:
        a = agent.CodexAdapter()
        a.binary = lambda: "/fake/codex"          # type: ignore[method-assign]
        argv = a.build_argv("SYSTEM", images=["/tmp/shot.png"])
        self.assertEqual(argv[-2:], ["-i", "/tmp/shot.png"])

    def test_there_is_no_second_model_and_no_network_call(self) -> None:
        """gpt-5.6-luna sees natively; nothing else is enrolled to look.

        OpenRouter is for text-to-speech only. A vision path that quietly grew
        an HTTP client would be the thing the user was most explicit about.
        """
        a = agent.CodexAdapter()
        a.binary = lambda: "/fake/codex"          # type: ignore[method-assign]
        argv = a.build_argv("SYSTEM", images=["/tmp/shot.png"])
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.6-luna")
        self.assertEqual(sum(1 for t in argv if t == "-m"), 1)
        self.assertFalse([t for t in argv if "openrouter" in t.lower()])
        source = Path(context.__file__).read_text(encoding="utf-8")
        for banned in ("openrouter", "urllib.request", "http.client"):
            self.assertNotIn(banned, source.lower())


class _Mute:
    speaking = False

    def say(self, text: str, wait: bool = False, timeout: float = 0.0):
        return {"spoken": text, "sentences": 1, "id": "x", "cancelled": False}

    def cancel(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"loaded": False, "speaking": False, "counters": {}}

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
