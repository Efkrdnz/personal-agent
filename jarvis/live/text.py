"""One Gemini text call, so the tidier has a model without the spine knowing about one.

:func:`jarvis.spec.tidy` takes a ``ModelCall`` protocol — ``(prompt) -> str``.
That seam is the reason the whole fidelity chain is testable in CI with no
credentials, and it is the reason this module is HERE rather than beside the
tidier: ``jarvis/spec.py`` is spine, must import under ``python -S``, and may
never know that Gemini exists. This package is the genai adapter, so this is
where a genai call belongs.

THE KEY IS A PARAMETER, ALWAYS. Like :class:`~jarvis.live.session.GenaiConnector`,
nothing here reads the environment: a process that silently picks up
``GEMINI_API_KEY`` from a shell is a process that bills somebody without being
asked, and the house rule is that credentials come from the keyring and are
passed down.

WHY NOT THE LIVE SESSION. Tidying is a one-shot text request with a response
schema, and the Live session is a duplex audio socket with a resumption handle.
Routing the tidier through it would make the read-back wait on a voice
connection, and would mean a build request typed into Telegram needed a
microphone to be tidied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["TEXT_MODEL", "GeminiText", "TextCallFailed"]

#: The text model for tidying. Deliberately NOT ``jarvis.live.MODEL``: that is the
#: LIVE model, a duplex audio endpoint, and asking it for a JSON document is
#: using the wrong door. This one is overridable per install because — unlike the
#: Live model, which was MEASURED — nobody has yet run the fidelity probe against
#: a specific text model here, and a pin nobody has tested should be easy to move.
TEXT_MODEL = "gemini-3-flash"


class TextCallFailed(RuntimeError):
    """The model did not answer, or answered with nothing usable.

    Its own type because the caller's response differs from a tidy that came back
    badly shaped: :class:`jarvis.spec.TidyFailed` means "the model drifted and the
    nets caught it", which is worth saying out loud to the user; this means "the
    network or the key is wrong", which is worth retrying.
    """


@dataclass
class GeminiText:
    """A ``jarvis.spec.ModelCall``: call it with a prompt, get the text back.

    ``google.genai`` is imported INSIDE :meth:`__call__`, never at module scope,
    so the package containing this file still imports on a machine with no
    third-party dependencies at all — which is what lets the whole voice layer be
    unit-tested without the extra installed.
    """

    api_key: str
    model: str = TEXT_MODEL
    #: Handed straight to the SDK. The tidier passes
    #: :data:`jarvis.spec.RESPONSE_SCHEMA`, a plain dict, so that no SDK type
    #: leaks into the spine's dependency-free half.
    response_schema: dict[str, Any] | None = None
    #: Zero, because this call is an EDITOR. Every requirement it emits must
    #: carry a span copied character-for-character out of the transcript, and
    #: sampling temperature is a licence to paraphrase the one thing that must
    #: not be paraphrased. The nets in jarvis.spec would catch the drift and
    #: throw the requirement away; this stops it being generated at all.
    temperature: float = 0.0
    client: Any | None = field(default=None, repr=False)

    def __call__(self, prompt: str) -> str:
        client = self.client
        if client is None:
            if not self.api_key:
                raise TextCallFailed("no Gemini credential: pass GeminiText(api_key=...)")
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - the extra is installed in CI
                raise TextCallFailed(
                    "google-genai is not installed: pip install '.[live]'"
                ) from exc
            client = genai.Client(api_key=self.api_key)

        config: dict[str, Any] = {"temperature": self.temperature}
        if self.response_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = self.response_schema
        try:
            reply = client.models.generate_content(model=self.model, contents=prompt, config=config)
        except Exception as exc:  # noqa: BLE001 - every SDK failure is one thing to the caller
            raise TextCallFailed(f"{type(exc).__name__}: {exc}") from exc

        text = getattr(reply, "text", None)
        if not text or not str(text).strip():
            # A blocked or empty response has a `text` of None and the reason
            # buried in candidates[0].finish_reason. Saying "the model returned
            # nothing" without that reason costs an afternoon.
            reason = ""
            for candidate in getattr(reply, "candidates", None) or ():
                if finish := getattr(candidate, "finish_reason", None):
                    reason = f" (finish_reason={getattr(finish, 'name', finish)})"
                    break
            raise TextCallFailed(f"the model returned no text{reason}")
        return str(text)
