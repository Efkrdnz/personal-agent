"""The startup assertion. Every test here is a reason NOT to start.

The value this guards against is not hypothetical: the shipped CLI accepts
``askUserQuestionTimeout`` of "60s", "5m", "10m" or "never", and anything but the
last two states of that list would auto-continue a question the user has not
answered. The symptom would be "Jarvis lost my answer", intermittently, with no
error anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.cc import settings


def write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_no_settings_files_at_all_is_the_supported_configuration(tmp_path: Path) -> None:
    # Unset means "blocks indefinitely", which is exactly what this design needs.
    settings.assert_no_ask_user_question_timeout([tmp_path / "nothing.json"])


def test_settings_without_the_key_are_fine(tmp_path: Path) -> None:
    path = write(tmp_path / "settings.json", {"model": "opus"})
    settings.assert_no_ask_user_question_timeout([path])


def test_never_is_the_one_value_that_is_allowed(tmp_path: Path) -> None:
    path = write(tmp_path / "settings.json", {"askUserQuestionTimeout": "never"})
    settings.assert_no_ask_user_question_timeout([path])


@pytest.mark.parametrize("value", sorted(settings.AUTO_CONTINUE_VALUES))
def test_every_value_the_cli_accepts_as_a_timeout_refuses_the_start(
    tmp_path: Path, value: str
) -> None:
    path = write(tmp_path / "managed-settings.json", {"askUserQuestionTimeout": value})
    with pytest.raises(settings.AskUserQuestionTimeoutSet) as e:
        settings.assert_no_ask_user_question_timeout([path])
    # The message names the FILE, because the whole point is that the setting may
    # be somewhere the user did not put it.
    assert str(path) in str(e.value)
    assert value in str(e.value)


def test_a_value_the_cli_does_not_recognise_is_also_refused(tmp_path: Path) -> None:
    # "probably falls back to the default" is not a proof, and this is the one
    # setting whose default the entire design depends on.
    path = write(tmp_path / "settings.json", {"askUserQuestionTimeout": "30m"})
    with pytest.raises(settings.AskUserQuestionTimeoutSet):
        settings.assert_no_ask_user_question_timeout([path])


def test_a_json_null_is_not_the_same_as_absent(tmp_path: Path) -> None:
    path = write(tmp_path / "settings.json", {"askUserQuestionTimeout": None})
    with pytest.raises(settings.AskUserQuestionTimeoutSet):
        settings.assert_no_ask_user_question_timeout([path])


def test_an_unparseable_settings_file_refuses_rather_than_assumes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"askUserQuestionTimeout": "never",}', encoding="utf-8")
    with pytest.raises(settings.UnreadableSettings) as e:
        settings.assert_no_ask_user_question_timeout([path])
    assert "impossible to prove" in str(e.value)


def test_a_settings_file_that_is_not_an_object_refuses_too(tmp_path: Path) -> None:
    path = write(tmp_path / "settings.json", ["not", "an", "object"])
    with pytest.raises(settings.UnreadableSettings):
        settings.assert_no_ask_user_question_timeout([path])


def test_the_managed_file_is_checked_before_the_users_own(tmp_path: Path) -> None:
    # Ordering is not cosmetic: the managed file is the one the user cannot
    # override, so naming it first names the actual cause.
    managed = write(tmp_path / "managed-settings.json", {"askUserQuestionTimeout": "60s"})
    user = write(tmp_path / "user.json", {"askUserQuestionTimeout": "5m"})
    with pytest.raises(settings.AskUserQuestionTimeoutSet) as e:
        settings.assert_no_ask_user_question_timeout([managed, user])
    assert e.value.path == managed


def test_the_search_path_covers_managed_user_project_and_local(tmp_path: Path) -> None:
    paths = settings.settings_files(
        tmp_path / "repo", platform="linux", home=tmp_path / "home", env={}
    )
    assert paths[0] == Path("/etc/claude-code/managed-settings.json")
    assert paths[1] == tmp_path / "home" / ".claude" / "settings.json"
    assert paths[2] == tmp_path / "repo" / ".claude" / "settings.json"
    assert paths[3] == tmp_path / "repo" / ".claude" / "settings.local.json"


def test_claude_config_dir_moves_the_user_settings_file(tmp_path: Path) -> None:
    paths = settings.settings_files(
        tmp_path, platform="linux", home=tmp_path, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    )
    assert paths[1] == tmp_path / "cfg" / "settings.json"


def test_the_managed_location_follows_the_platform() -> None:
    assert settings.settings_files(platform="darwin")[0] == Path(
        "/Library/Application Support/ClaudeCode/managed-settings.json"
    )
    assert settings.settings_files(platform="win32")[0] == Path(
        "C:/Program Files/ClaudeCode/managed-settings.json"
    )
