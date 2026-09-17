"""Presence, never value — and a headless box is a fallback, not a failure.

The one behaviour that must not regress: nothing here ever puts a credential in
a string a human or a log can read. The second: ``store`` refuses rather than
inventing a file to write to when the keyring is absent.
"""

from __future__ import annotations

import pytest

from jarvis import secrets


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the keyring OFF so the environment half is what is under test."""
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: None)
    for s in secrets.SECRETS:
        monkeypatch.delenv(s.env, raising=False)


def test_an_unknown_name_names_the_known_ones() -> None:
    with pytest.raises(KeyError, match="gemini_api_key"):
        secrets.get("gemini_key")


def test_the_environment_is_the_documented_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "abc123")
    assert secrets.get("gemini_api_key") == "abc123"
    assert secrets.probe()[0].source == "environment"


def test_the_keyring_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "from-env")
    monkeypatch.setattr(secrets, "_from_keyring", lambda secret: "from-keyring")
    assert secrets.get("gemini_api_key") == "from-keyring"


def test_a_missing_required_secret_carries_its_own_instructions() -> None:
    with pytest.raises(secrets.MissingSecret) as exc:
        secrets.require("gemini_api_key")
    message = str(exc.value)
    assert "aistudio.google.com" in message  # how to get one
    assert "python -m jarvis secrets set gemini_api_key" in message  # how to store it
    assert "JARVIS_GEMINI_API_KEY" in message  # the headless route


def test_probe_reports_presence_and_never_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_GEMINI_API_KEY", "sk-do-not-print-me")
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "123:also-secret")
    rendered = "\n".join(p.line() for p in secrets.probe())
    assert "do-not-print-me" not in rendered
    assert "also-secret" not in rendered
    assert "gemini_api_key" in rendered


def test_an_optional_secret_is_a_feature_being_off_not_a_problem() -> None:
    by_name = {p.secret.name: p for p in secrets.probe()}
    assert by_name["gemini_api_key"].blocking is True
    assert by_name["github_token"].blocking is False
    assert "feature is off" in by_name["github_token"].line()


def test_the_mark_and_the_detail_do_not_repeat_each_other() -> None:
    found = secrets.probe()[0]
    assert found.mark == "MISSING"
    assert not found.detail().startswith("MISSING")
    assert found.line().startswith("MISSING")


def test_storing_an_empty_value_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        secrets.store("gemini_api_key", "   ")


def test_store_does_not_invent_a_file_when_there_is_no_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of this module: no fallback to writing a secret to disk."""
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *a: object, **kw: object) -> object:
        if name == "keyring":
            raise ImportError("no keyring here")
        return real_import(name, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(ImportError):
        secrets.store("gemini_api_key", "abc")


def test_forget_is_false_rather_than_an_error_when_nothing_is_there() -> None:
    assert secrets.forget("github_token") is False


def test_the_spine_rule_holds_no_third_party_import_at_module_scope() -> None:
    """Rule 4. Every process opens this module, including ones with no packages."""
    import ast
    from pathlib import Path

    source = Path(secrets.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top_level if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module or "" for n in top_level if isinstance(n, ast.ImportFrom)}
    assert "keyring" not in names
