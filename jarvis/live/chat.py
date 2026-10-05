"""Jarvis by text: the ordinary Gemini API, the same tools as the voice.

The desk is a duplex audio socket. This is the other door — one request, one
answer — for a terminal, a script, or a machine with no microphone at all, and
it has to reach EXACTLY the same capabilities: weather, reminders, notes, web
search, Claude Code. So it takes the registry's own function declarations and a
``dispatch`` callable, and nothing here knows what any tool does.

THE TOOL LOOP IS OURS, NOT THE SDK'S. google-genai can call Python functions
itself ("automatic function calling"), and that is switched OFF: it would call
handlers directly, past :meth:`jarvis.tools.registry.Registry.dispatch`, which
is where the channel gate, the argument check and the activity log live. A tool
the CLI may not call must be refused by code here exactly as on the phone.

THOUGHT SIGNATURES. Newer Gemini models attach a signature to the turn that
makes a function call and require it back on the next request. The model's own
``Content`` is appended to the history UNCHANGED for that reason — rebuilding
it from the function call's name and arguments would drop the signature and
the next request would be refused.

THE KEY IS A PARAMETER. See :mod:`jarvis.live.text`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis.live.persona import DEFAULT_ADDRESS, text_instruction
from jarvis.live.text import TEXT_MODEL, TextCallFailed

__all__ = ["PERSONA", "ChatTurn", "GeminiChat", "persona"]

#: Who Jarvis is, in any channel that is not a phone call to a third party,
#: with the default address. General first: the build console is one of the
#: things it does, not the thing it is. The words live in jarvis.live.persona,
#: so the text chat and the voice cannot drift into two different characters.
PERSONA = text_instruction()


def persona(
    extra: str = "",
    *,
    address: str = DEFAULT_ADDRESS,
    name: str = "",
    builds: bool = True,
    pc: bool = False,
    vision: bool = False,
    commands: bool = False,
) -> str:
    """The persona for this user, plus anything channel- or user-specific (their notes, say)."""
    told = text_instruction(address, name, builds=builds, pc=pc, vision=vision, commands=commands)
    return "\n\n".join(x for x in (told, extra) if x)


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One user message's outcome: the reply, and every tool it took to get there."""

    text: str
    tools: tuple[tuple[str, str], ...] = ()  # (tool name, what it returned)


@dataclass
class GeminiChat:
    """A conversation over ``generate_content``, holding its own history.

    ``dispatch(name, args) -> str`` runs a tool and never raises — that is
    :meth:`Registry.dispatch`'s contract — so a broken tool becomes a sentence
    the model can read and relay, not an exception that ends the chat.
    """

    api_key: str
    declarations: Sequence[Mapping[str, Any]]
    dispatch: Callable[[str, dict[str, Any]], str]
    system_instruction: str = PERSONA
    model: str = TEXT_MODEL
    #: Tool rounds per user message. A model that calls tools forever is a bill,
    #: not a conversation; past this it is told to answer with what it has.
    max_rounds: int = 6
    #: Contents kept, oldest dropped first, never splitting a call from its
    #: response — a function response with no call before it is refused.
    max_history: int = 40
    temperature: float = 0.6
    client: Any | None = field(default=None, repr=False)
    history: list[Any] = field(default_factory=list, repr=False)

    def _client(self) -> Any:
        if self.client is None:
            if not self.api_key:
                raise TextCallFailed("no Gemini credential: pass GeminiChat(api_key=...)")
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - the extra is installed in CI
                raise TextCallFailed(
                    'google-genai is not installed: pip install -e ".[live]"'
                ) from exc
            self.client = genai.Client(api_key=self.api_key)
        return self.client

    def _config(self, *, tools: bool) -> Any:
        from google.genai import types as t

        decls = [t.FunctionDeclaration(**dict(d)) for d in self.declarations] if tools else []
        return t.GenerateContentConfig(
            system_instruction=self.system_instruction or None,
            temperature=self.temperature,
            tools=[t.Tool(function_declarations=decls)] if decls else None,
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )

    def send(self, text: str) -> ChatTurn:
        from google.genai import types as t

        client = self._client()
        self.history.append(t.Content(role="user", parts=[t.Part.from_text(text=text)]))
        used: list[tuple[str, str]] = []
        for round_ in range(self.max_rounds + 1):
            last = round_ == self.max_rounds
            try:
                reply = client.models.generate_content(
                    model=self.model, contents=self.history, config=self._config(tools=not last)
                )
            except Exception as exc:  # noqa: BLE001 - every SDK failure is one thing here
                self._forget_unanswered()
                raise TextCallFailed(f"{type(exc).__name__}: {exc}") from exc

            content = _first_content(reply)
            calls = (
                [p.function_call for p in (content.parts or []) if p.function_call]
                if (content is not None)
                else []
            )
            if content is not None:
                self.history.append(content)
            if not calls:
                answer = str(getattr(reply, "text", "") or "").strip()
                if not answer:
                    answer = _why_empty(reply)
                self._trim()
                return ChatTurn(answer, tuple(used))

            responses = []
            for call in calls:
                args = dict(call.args or {})
                said = self.dispatch(call.name, args)
                used.append((call.name, str(said)))
                response: dict[str, Any] = {"result": str(said)}
                # A Reply's detail is what the model answers from (a command's
                # output, what is on the screen); the result is only its summary.
                detail = getattr(said, "detail", "")
                if detail:
                    response["detail"] = detail
                responses.append(
                    t.Part(
                        function_response=t.FunctionResponse(
                            id=call.id, name=call.name, response=response
                        )
                    )
                )
            self.history.append(t.Content(role="user", parts=responses))
        # Unreachable: the last round offers no tools, so it cannot call one.
        raise TextCallFailed("the model kept calling tools")  # pragma: no cover

    def reset(self) -> None:
        self.history.clear()

    def _forget_unanswered(self) -> None:
        """Drop a trailing user message the model never answered, so a retry is clean."""
        while self.history and getattr(self.history[-1], "role", "") == "user":
            self.history.pop()
            if self.history and _is_call(self.history[-1]):
                self.history.pop()
            else:
                break

    def _trim(self) -> None:
        while len(self.history) > self.max_history:
            self.history.pop(0)
            # Never start on a model turn or a function response: both need
            # the user turn before them, and the API refuses the orphan.
            while self.history and not _is_plain_user(self.history[0]):
                self.history.pop(0)


def _first_content(reply: Any) -> Any:
    for cand in getattr(reply, "candidates", None) or ():
        if getattr(cand, "content", None) is not None:
            return cand.content
    return None


def _is_call(content: Any) -> bool:
    return any(getattr(p, "function_call", None) for p in (getattr(content, "parts", None) or ()))


def _is_plain_user(content: Any) -> bool:
    if getattr(content, "role", "") != "user":
        return False
    parts = getattr(content, "parts", None) or ()
    return not any(getattr(p, "function_response", None) for p in parts)


def _why_empty(reply: Any) -> str:
    for cand in getattr(reply, "candidates", None) or ():
        if reason := getattr(cand, "finish_reason", None):
            return f"(no answer: {getattr(reason, 'name', reason)})"
    return "(no answer)"
