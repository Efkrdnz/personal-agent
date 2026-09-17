"""Dependencies point inwards, and this is what fails when they stop.

The spine's answer-shape helpers spent a stage inside :mod:`jarvis.cc`, so the
Telegram channel and the voice layer both imported the Claude Code driver's
package to reach a pure function. Every test passed. The seam claims in CLAUDE.md
were false and nothing said so.

This file says so. It also tests the CHECKER on a planted violation, because a
layering guard that silently finds no files is the most comfortable kind of
broken.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.check_layers import (  # noqa: E402 - the repo root is not on sys.path at import time
    COMPOSITION_ROOTS,
    RULES,
    imports_of,
    modules_of,
    violations,
)


@pytest.mark.parametrize("layer", sorted(RULES))
def test_there_is_something_to_check(layer: str) -> None:
    assert modules_of(layer, ROOT), f"{layer} has no modules; its rule would pass vacuously"


@pytest.mark.parametrize("layer", sorted(RULES))
def test_no_layer_imports_a_layer_it_must_not_know_about(layer: str) -> None:
    prefixes = RULES[layer]
    bad = [
        f"{path.relative_to(ROOT).as_posix()} imports {name}"
        for path in modules_of(layer, ROOT)
        for name in sorted(imports_of(path, ROOT))
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    ]
    assert not bad, "\n".join(bad)


def test_the_whole_tree_is_clean_right_now() -> None:
    assert violations(ROOT) == []


def test_the_spine_does_not_import_the_driver_the_answer_shapes_came_from() -> None:
    """The specific regression. ``jarvis.answers`` is the spine's, not the driver's."""
    assert not any(name.startswith("jarvis.cc") for name in imports_of(ROOT / "jarvis/answers.py"))


def test_the_driver_never_reaches_a_channel_or_a_sound_card() -> None:
    """The other direction of the same seam, which nothing else in the suite pins.

    ``jarvis/cc/`` is its own OS process and never speaks. A local import of
    :mod:`jarvis.voice` there would break no test and no run — it would just mean
    the driver could no longer start on a box with no audio stack, which is every
    box the phone leg will ever run on.
    """
    reached = [
        f"{path.relative_to(ROOT).as_posix()} imports {name}"
        for path in modules_of("jarvis/cc", ROOT)
        for name in sorted(imports_of(path, ROOT))
        if name.startswith(("jarvis.voice", "jarvis.audio", "jarvis.live", "jarvis.telegram"))
    ]
    assert not reached, "\n".join(reached)


def test_a_docstring_naming_another_layer_is_not_a_violation() -> None:
    """Half these modules name other layers in their prose on purpose.

    ``jarvis.voice.chunk`` documents where the numbering lives, which is the only
    reason a reader finds it. A grep-based guard would fail on correct prose and
    would then be deleted, which is how this class of check dies.
    """
    chunk = ROOT / "jarvis/voice/chunk.py"
    assert "jarvis.answers" in chunk.read_text(), "the pointer to the spine's numbering is gone"
    assert not any(name.startswith("jarvis.cc") for name in imports_of(chunk))


def test_an_import_inside_a_function_does_not_slip_past(tmp_path: Path) -> None:
    """The way somebody actually reaches across a seam: locally, "just this once"."""
    pkg = tmp_path / "jarvis" / "voice"
    pkg.mkdir(parents=True)
    (pkg / "sneaky.py").write_text(
        "def go() -> None:\n    from jarvis.cc import gate  # noqa: F401\n"
    )
    assert "jarvis.cc" in imports_of(pkg / "sneaky.py", tmp_path)

    (pkg / "sneaky.py").write_text("def go() -> None:\n    import jarvis.telegram.bot\n")
    assert "jarvis.telegram.bot" in imports_of(pkg / "sneaky.py", tmp_path)


def test_a_relative_import_is_resolved_rather_than_ignored(tmp_path: Path) -> None:
    """``from .. import cc`` is the same reach as naming the package outright."""
    pkg = tmp_path / "jarvis" / "voice"
    pkg.mkdir(parents=True)
    (pkg / "relative.py").write_text("from .. import cc  # noqa: F401\n")
    assert "jarvis.cc" in imports_of(pkg / "relative.py", tmp_path)

    (pkg / "relative.py").write_text("from ..cc import gate  # noqa: F401\n")
    assert "jarvis.cc.gate" in imports_of(pkg / "relative.py", tmp_path)


def test_a_module_whose_name_merely_starts_the_same_is_not_a_violation(tmp_path: Path) -> None:
    # jarvis.ccache would be a new spine module, not the driver.
    pkg = tmp_path / "jarvis" / "voice"
    pkg.mkdir(parents=True)
    (pkg / "near.py").write_text("import jarvis.ccache  # noqa: F401\n")
    names = imports_of(pkg / "near.py", tmp_path)
    assert not any(n == "jarvis.cc" or n.startswith("jarvis.cc.") for n in names)


def test_the_checker_reports_a_planted_violation(tmp_path: Path) -> None:
    """End to end, through the same entry point CI runs."""
    # Every layer in RULES, derived rather than listed: a layer added to the
    # guard and forgotten here would make its own rule report "vacuously" and
    # fail this test for a reason that has nothing to do with the planted import.
    for part in (layer for layer in RULES if layer != "spine"):
        (tmp_path / part).mkdir(parents=True, exist_ok=True)
        (tmp_path / part / "__init__.py").write_text("")
    (tmp_path / "jarvis" / "answers.py").write_text("from jarvis.voice import script\n")
    (tmp_path / "jarvis" / "telegram" / "channel.py").write_text("from jarvis.cc import narrate\n")
    (tmp_path / "jarvis" / "cc" / "driver.py").write_text("from jarvis.voice import chunk\n")

    found = violations(tmp_path)
    assert any("jarvis/answers.py" in line and "jarvis.voice" in line for line in found)
    assert any("jarvis/telegram/channel.py" in line and "jarvis.cc" in line for line in found)
    assert any("jarvis/cc/driver.py" in line and "jarvis.voice" in line for line in found)
    # Every rule had files, so no line here is the vacuity warning.
    assert not any("vacuously" in line for line in found)


def test_the_project_lifecycle_sits_above_github_and_below_everything_that_speaks() -> None:
    """Stage 4's seam, in both directions, because one direction is not a seam.

    The lifecycle may reach DOWN to the spine and to the GitHub client. What it
    must never do is reach UP: a lifecycle that imported ``jarvis.cc`` would make
    stage 6's "the phone touches zero files in jarvis/cc" claim false by proxy,
    and one that imported a channel could only ever have its read-back answered
    on that channel.
    """
    reached = [
        f"{path.relative_to(ROOT).as_posix()} imports {name}"
        for path in modules_of("jarvis/project", ROOT)
        for name in sorted(imports_of(path, ROOT))
        if name.startswith(
            ("jarvis.cc", "jarvis.voice", "jarvis.audio", "jarvis.live", "jarvis.telegram")
        )
    ]
    assert not reached, "\n".join(reached)

    # And it DOES use the two layers below it, so the rule is not passing because
    # the package happens to import nothing at all.
    imported = {
        name for path in modules_of("jarvis/project", ROOT) for name in imports_of(path, ROOT)
    }
    assert any(n.startswith("jarvis.github") for n in imported)
    assert "jarvis.effects" in imported


def test_nothing_below_the_project_lifecycle_imports_it() -> None:
    """The other half. The spine and the provider adapter must not know it exists."""
    for layer in ("spine", "jarvis/github"):
        reached = [
            f"{path.relative_to(ROOT).as_posix()} imports {name}"
            for path in modules_of(layer, ROOT)
            for name in sorted(imports_of(path, ROOT))
            if name == "jarvis.project" or name.startswith("jarvis.project.")
        ]
        assert not reached, "\n".join(reached)


def test_the_briefing_does_not_know_how_it_is_delivered() -> None:
    """Stage 5's seam. A section is a row; which channel says it is decided elsewhere.

    An import of the desk here would mean the briefing could only ever be spoken,
    an import of Telegram that it could only ever be tapped, and stage 6's phone
    would be a rewrite rather than one more channel reading the same rows.
    """
    reached = [
        f"{path.relative_to(ROOT).as_posix()} imports {name}"
        for path in modules_of("jarvis/briefing", ROOT)
        for name in sorted(imports_of(path, ROOT))
        if name.startswith(
            ("jarvis.cc", "jarvis.voice", "jarvis.audio", "jarvis.live", "jarvis.telegram")
        )
    ]
    assert not reached, "\n".join(reached)

    # And it DOES reach down to both layers below it, so the rule is not passing
    # because the package happens to import nothing at all.
    imported = {
        name for path in modules_of("jarvis/briefing", ROOT) for name in imports_of(path, ROOT)
    }
    assert any(n.startswith("jarvis.github") for n in imported)
    assert "jarvis.requests" in imported
    assert "jarvis.reconcile" in imported


def test_nothing_below_the_briefing_imports_it() -> None:
    """The other direction, which is the half that actually decays.

    ``jarvis.reconcile.project_status`` composes the briefing's first section, so
    the spine is exactly where a back-import would feel natural and be wrong: the
    spine has to keep importing under ``python -S`` on a box with no network.
    """
    for layer in ("spine", "jarvis/github"):
        reached = [
            f"{path.relative_to(ROOT).as_posix()} imports {name}"
            for path in modules_of(layer, ROOT)
            for name in sorted(imports_of(path, ROOT))
            if name == "jarvis.briefing" or name.startswith("jarvis.briefing.")
        ]
        assert not reached, "\n".join(reached)


def test_the_tool_surface_knows_nothing_about_who_called_it() -> None:
    """A tool is dispatched from the desk, from Telegram and from a phone call.

    So it may not import any of them, nor the driver, nor a sound card: what it
    knows about its caller is ``ctx.channel``, a string, and the capability that
    implies is the ``channels`` column on the tool. An import of the voice layer
    here would be a tool that works at the desk and nowhere else — the exact
    shape of the reference build's ``ctx["player"]``.
    """
    reached = [
        f"{path.relative_to(ROOT).as_posix()} imports {name}"
        for path in modules_of("jarvis/tools", ROOT)
        for name in sorted(imports_of(path, ROOT))
        if name.startswith(
            ("jarvis.cc", "jarvis.voice", "jarvis.audio", "jarvis.live", "jarvis.telegram")
        )
    ]
    assert not reached, "\n".join(reached)

    # And it DOES reach down, so the rule is not passing vacuously.
    imported = {
        name for path in modules_of("jarvis/tools", ROOT) for name in imports_of(path, ROOT)
    }
    assert "jarvis.jobs" in imported
    assert any(n.startswith("jarvis.project") for n in imported)


def test_nothing_below_the_tool_surface_imports_it() -> None:
    for layer in ("spine", "jarvis/github", "jarvis/project", "jarvis/cc"):
        reached = [
            f"{path.relative_to(ROOT).as_posix()} imports {name}"
            for path in modules_of(layer, ROOT)
            for name in sorted(imports_of(path, ROOT))
            if name == "jarvis.tools" or name.startswith("jarvis.tools.")
        ]
        assert not reached, "\n".join(reached)


def test_there_is_exactly_one_composition_root() -> None:
    """The exemption is a hole in the guard, so the hole has a fixed size.

    A composition root is allowed to import every layer because wiring them
    together is its whole job. That is only safe while there is ONE of them and
    it contains no decisions — so adding a second is a change to this line, in a
    diff somebody has to agree with, rather than a file that quietly appears.
    """
    assert sorted(COMPOSITION_ROOTS) == ["jarvis/__main__.py"]


def test_the_composition_root_is_not_checked_as_spine() -> None:
    """The exemption actually applies: the desk app is not in the spine's file list."""
    root = ROOT / "jarvis/__main__.py"
    if not root.exists():  # pragma: no cover - it does
        pytest.skip("no composition root yet")
    assert root not in modules_of("spine", ROOT)
    # ...and it really does import things the spine may not, so the exemption is
    # load-bearing rather than decorative.
    names = imports_of(root, ROOT)
    assert any(n.startswith(("jarvis.live", "jarvis.audio", "jarvis.voice")) for n in names)
