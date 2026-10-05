"""Jarvis.exe is built from these files, and nothing runs them until a Windows runner does.

A PyInstaller spec, a workflow and an icon have the same failure mode as this
repo's other bugs: they live BETWEEN layers. A data directory the spec does not
name builds fine, starts fine, and fails in front of the user the first time
the window or the database is opened. A workflow with a typo fails on GitHub,
where nobody running pytest is looking. So everything here is read as text and
checked against the tree it has to agree with — no PyInstaller, no network,
no Windows.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import re
import struct
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "jarvis.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-app.yml"
ICO = ROOT / "packaging" / "jarvis.ico"
ANNOTATE = ROOT / "packaging" / "ci_annotate.py"

sys.path.insert(0, str(ROOT))
from tools.check_layers import RULES, violations  # noqa: E402 - the repo root is not on sys.path

# ───────────────────────────── helpers ─────────────────────────────


def _spec_tree() -> ast.Module:
    return ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))


def _spec_constant(name: str) -> Any:
    """A module-level literal from the spec, read without running PyInstaller."""
    for node in _spec_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"jarvis.spec has no literal {name}")


def _spec_function(name: str, namespace: dict[str, Any]) -> Any:
    """One function from the spec, compiled on its own, so its logic can be tested."""
    for node in _spec_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            exec(compile(ast.Module([node], []), str(SPEC), "exec"), namespace)  # noqa: S102
            return namespace[name]
    raise AssertionError(f"jarvis.spec has no function {name}")


def _data_dirs_in_tree() -> set[str]:
    """Every directory under jarvis/ holding a file the interpreter does not import."""
    found = set()
    for path in (ROOT / "jarvis").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in {".py", ".pyc", ".pyi"} or path.name == "py.typed":
            continue
        found.add(path.parent.relative_to(ROOT).as_posix())
    return found


def _workflow() -> dict[Any, Any]:
    # Not importorskip: a workflow test that skips itself is a workflow nobody
    # checks. pyyaml is in the dev extra.
    import yaml

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: str = "build") -> list[dict[str, Any]]:
    return _workflow()["jobs"][job]["steps"]


def _step(name_or_id: str) -> dict[str, Any]:
    for step in _steps():
        if name_or_id in (step.get("id"), step.get("name")):
            return step
    raise AssertionError(f"the workflow has no step {name_or_id!r}")


def _annotate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_annotate", ANNOTATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# ───────────────────────────── the spec ─────────────────────────────


def test_the_spec_parses() -> None:
    names = {
        node.func.id
        for node in ast.walk(_spec_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"Analysis", "PYZ", "EXE", "COLLECT"} <= names


def test_the_spec_names_every_data_dir_that_exists() -> None:
    """A data dir left out builds, starts, and fails in front of the user."""
    named = {rel for rel, _glob in _spec_constant("DATA_DIRS")}
    assert named == _data_dirs_in_tree(), (
        "packaging/jarvis.spec DATA_DIRS must list exactly the directories under jarvis/ "
        "that hold non-Python files"
    )


def test_every_data_file_is_matched_by_its_dirs_glob() -> None:
    globs = dict(_spec_constant("DATA_DIRS"))
    for rel, pattern in globs.items():
        files = sorted(p for p in (ROOT / rel).iterdir() if p.is_file())
        assert files, f"{rel} is empty"
        missed = [p.name for p in files if not p.match(pattern)]
        assert not missed, f"{rel}/{pattern} would leave out {missed}"


def test_the_data_the_frozen_app_reads_is_where_the_spec_puts_it() -> None:
    """The destinations are the source paths, because the code finds them via __file__."""
    from jarvis import db
    from jarvis.window import server

    named = {rel for rel, _ in _spec_constant("DATA_DIRS")}
    assert db.MIGRATIONS_DIR.relative_to(ROOT).as_posix() in named
    assert server.STATIC_DIR.relative_to(ROOT).as_posix() in named


def test_the_exe_is_windowed_named_jarvis_and_has_the_icon() -> None:
    exe = next(
        node
        for node in ast.walk(_spec_tree())
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "EXE"
    )
    kw = {k.arg: k.value for k in exe.keywords}
    assert ast.literal_eval(kw["console"]) is False, "a console window is what the user hated"
    assert ast.literal_eval(kw["name"]) == "Jarvis"
    assert "jarvis.ico" in ast.unparse(kw["icon"])
    assert ast.literal_eval(kw["exclude_binaries"]) is True, "one folder, not one file"
    assert ast.literal_eval(kw["upx"]) is False


def test_the_spec_starts_the_same_entry_as_the_pip_launcher() -> None:
    """One function runs the app and every child, frozen or not."""
    text = SPEC.read_text(encoding="utf-8")
    assert '"jarvis_app.py"' in text
    entry = ROOT / "packaging" / "jarvis_app.py"
    assert entry.is_file(), "the spec's entry script is missing"
    imported = {
        (node.module, alias.name)
        for node in ast.walk(ast.parse(entry.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    module, _, fn = _pyproject()["project"]["gui-scripts"]["Jarvis"].partition(":")
    assert (module, fn) in imported


def test_the_spec_collects_what_static_analysis_cannot_see() -> None:
    packages = dict(_spec_constant("PACKAGES"))
    for name in (
        "claude_agent_sdk",  # _bundled/claude.exe
        "onnxruntime",
        "_sounddevice_data",  # the PortAudio DLL
        "soxr",
        "tzdata",
        "certifi",
        "google.genai",
        "keyring",
    ):
        assert packages.get(name) is True, f"{name} must be collected, and required on Windows"
    assert "miniaudio" in _spec_constant("MODULES")
    text = SPEC.read_text(encoding="utf-8")
    assert 'collect_submodules("jarvis")' in text, "lazy jarvis.* imports would be missed"
    assert 'collect_submodules("keyring.backends")' in text
    assert 'copy_metadata("keyring")' in text, "keyring finds backends via entry points"
    assert '"pystray._win32"' in text


def test_the_wake_models_never_enter_the_bundle() -> None:
    """ADR 0012: CC BY-NC-SA models are downloaded at run time, never shipped."""
    assert "openwakeword" in _spec_constant("EXCLUDES")
    never = _spec_constant("NEVER_BUNDLE")
    wake = ast.parse((ROOT / "jarvis" / "audio" / "wake.py").read_text(encoding="utf-8"))
    models = next(
        ast.literal_eval(node.value)
        for node in wake.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "MODELS"
    )
    for name in models:
        assert any(part in name for part in never), f"{name} is not refused by the spec"


def test_the_spec_refuses_a_build_that_carries_a_wake_model() -> None:
    refuse = _spec_function(
        "_refuse_wake_models", {"Path": Path, "NEVER_BUNDLE": _spec_constant("NEVER_BUNDLE")}
    )
    refuse([("jarvis/window/static/app.js", "/src/jarvis/window/static/app.js", "DATA")])
    with pytest.raises(SystemExit, match="ADR|adr"):
        refuse([("wake/hey_jarvis_v0.1.onnx", "/home/u/.local/share/hey_jarvis_v0.1.onnx", "DATA")])
    with pytest.raises(SystemExit):
        refuse([("x/res.onnx", "/site-packages/openwakeword/resources/x.onnx", "DATA")])


# ───────────────────────────── pyproject ─────────────────────────────


def test_the_gui_scripts_entry_resolves_to_an_importable_callable() -> None:
    target = _pyproject()["project"]["gui-scripts"]["Jarvis"]
    module, _, attr = target.partition(":")
    assert (module, attr) == ("jarvis.app.entry", "main")
    fn = getattr(importlib.import_module(module), attr)
    assert callable(fn)


def test_the_extras_the_app_needs_exist() -> None:
    project = _pyproject()["project"]
    extras = {k: " ".join(v) for k, v in project["optional-dependencies"].items()}
    assert "miniaudio" in extras["tts"], "Windows has no ffmpeg; miniaudio decodes Edge's MP3"
    assert "edge-tts" in extras["tts"]
    assert "pystray" in extras["app"] and "pillow" in extras["app"]
    assert "pyinstaller" in extras["build"]
    assert "pyyaml" in extras["dev"], "this file parses the workflow"
    assert "tzdata; sys_platform == 'win32'" in project["dependencies"]


# ───────────────────────────── the workflow ─────────────────────────────


def test_the_workflow_parses_and_runs_on_windows() -> None:
    wf = _workflow()
    # YAML 1.1 reads a bare `on` as the boolean True.
    on = wf.get("on", wf.get(True))
    assert "workflow_dispatch" in on
    push = on["push"]
    assert "v*" in push["tags"]
    assert push["branches"], "with only `tags`, a branch push would never run this"
    for path in (
        "jarvis/**",
        "packaging/**",
        "pyproject.toml",
        ".github/workflows/windows-app.yml",
    ):
        assert path in push["paths"]
    build = wf["jobs"]["build"]
    assert build["runs-on"] == "windows-latest"
    setup = next(
        s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/setup-python")
    )
    assert str(setup["with"]["python-version"]) == "3.12"


def test_the_workflow_installs_every_extra_the_exe_carries() -> None:
    script = _step("install")["run"]
    extras = re.search(r'-e "\.\[([^\]]+)\]"', script)
    assert extras, "the install line is not where this test expects it"
    have = set(extras.group(1).split(","))
    assert {"cc", "voice", "live", "tts", "geo", "wake", "secrets", "app", "build", "dev"} <= have
    assert have <= set(_pyproject()["project"]["optional-dependencies"])


def test_the_workflow_runs_the_tests_builds_the_spec_and_selftests_the_exe() -> None:
    assert "pytest" in _step("tests")["run"]
    assert "packaging/jarvis.spec" in _step("build")["run"]
    selftest = _step("selftest")["run"]
    for word in ("Jarvis.exe", "--selftest", "--report", "Start-Process", "ExitCode", "JARVIS_LOG"):
        assert word in selftest, f"the selftest step lost {word}"


def test_every_step_that_runs_something_keeps_its_output_in_ci_logs() -> None:
    """The check-run API returns annotations, not logs. A step whose output is not
    in ci-logs/ cannot be reported when it fails."""
    for step in _steps():
        script = step.get("run", "")
        if not script or "ci_annotate.py" in script:
            continue
        assert "ci-logs" in script, f"step {step.get('name') or step.get('id')} logs nowhere"
        if "python -m" in script:
            assert "Tee-Object" in script, f"{step.get('id')} runs python without a tee"


def test_a_failure_is_reported_as_annotations_after_every_step_that_logs() -> None:
    steps = _steps()
    idx = next(
        i for i, s in enumerate(steps) if "ci_annotate.py failure ci-logs" in s.get("run", "")
    )
    assert steps[idx]["if"].strip() == "failure()"
    logging = [i for i, s in enumerate(steps) if "ci-logs/" in s.get("run", "")]
    assert idx > max(i for i in logging if "ci_annotate" not in steps[i].get("run", ""))


def test_a_green_run_still_reports_the_selftest_as_a_notice() -> None:
    runs = " ".join(s.get("run", "") for s in _steps())
    assert "ci_annotate.py notice selftest ci-logs/selftest.json" in runs
    assert "--report', 'ci-logs/selftest.json'" in _step("selftest")["run"]


def test_every_log_gets_its_own_annotation() -> None:
    """The runner keeps ten errors per step; more log files would drop the overflow."""
    text = WORKFLOW.read_text(encoding="utf-8")
    files = set(re.findall(r"ci-logs/([\w.-]+\.(?:log|json|txt))", text))
    assert files, "no log files found in the workflow"
    assert len(files) <= _annotate().MAX_PER_STEP


def test_nothing_is_uploaded_unless_the_selftest_and_the_licence_check_passed() -> None:
    zip_if = _step("zip")["if"]
    assert "steps.selftest.outcome == 'success'" in zip_if
    assert "steps.licence.outcome == 'success'" in zip_if
    uploads = [
        s for s in _steps() if str(s.get("uses", "")).startswith("actions/upload-artifact@v4")
    ]
    app = next(s for s in uploads if s["with"]["name"] == "Jarvis-Windows-x64")
    assert "steps.zip.outcome == 'success'" in app["if"]


def test_only_the_release_job_can_write_to_the_repository() -> None:
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in wf["jobs"]["build"]
    release = wf["jobs"]["release"]
    assert release["permissions"] == {"contents": "write"}
    assert release["needs"] == "build"
    assert "refs/tags/v" in release["if"]
    assert "workflow_dispatch" in release["if"] and "inputs.release" in release["if"]


def test_a_release_can_be_asked_for_by_hand_and_makes_its_own_tag() -> None:
    # For when only the Actions API is reachable: the run builds, tests and
    # selftests as always, and the release job creates the tag on that commit.
    wf = _workflow()
    on = wf.get("on", wf.get(True))
    release_input = on["workflow_dispatch"]["inputs"]["release"]
    assert release_input["default"] == "" and release_input["required"] is False
    publish = next(
        s for s in wf["jobs"]["release"]["steps"] if s.get("name") == "Publish the release"
    )
    script = publish["run"]
    assert "^v[0-9]+\\.[0-9]+\\.[0-9]+$" in script, "a typed tag is checked before it is used"
    assert '--target "$GITHUB_SHA"' in script and "--verify-tag" in script
    hand_off = next(
        s
        for s in wf["jobs"]["build"]["steps"]
        if s.get("name") == "Hand the zip to the release job"
    )
    assert "inputs.release" in hand_off["if"] and "steps.zip.outcome == 'success'" in hand_off["if"]
    # A release is never cancelled by, and never cancels, a plain build.
    assert "inputs.release" in wf["concurrency"]["group"]
    assert "inputs.release" in wf["concurrency"]["cancel-in-progress"]


def test_nothing_typed_or_sent_from_outside_is_pasted_into_a_script() -> None:
    # ${{ }} in a run: block is substituted before the shell sees it, so a
    # crafted input or branch name would run as code. The environment carries
    # such values instead.
    wf = _workflow()
    for job in wf["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            for source in ("inputs.", "github.event.", "github.head_ref", "github.ref_name"):
                assert "${{ " + source not in run and "${{" + source not in run, (
                    step.get("name"),
                    source,
                )


# ───────────────────────────── the annotations ─────────────────────────────


def test_annotations_escape_what_github_would_misread() -> None:
    a = _annotate()
    line = a.command("error", "a:b,c", "50% done\r\nnext")
    assert line == "::error title=a%3Ab%2Cc::50%25 done%0D%0Anext"


def test_a_long_log_keeps_its_end_within_the_runners_limit() -> None:
    a = _annotate()
    text = "\n".join(f"line {i} " + "x" * 120 for i in range(500)) + "\nTHE LAST LINE"
    out = a.tail(text)
    assert out.endswith("THE LAST LINE")
    assert len(out) <= a.MAX_CHARS
    assert len(out.splitlines()) <= a.TAIL_LINES
    assert len(a.tail("y" * 10_000).splitlines()[0]) <= 400


def test_a_selftest_report_puts_failures_first_and_shrinks_passes_before_failures() -> None:
    a = _annotate()
    big = {f"check {i}": {"ok": True, "detail": "d" * 400, "critical": True} for i in range(20)}
    big["portaudio"] = {"ok": False, "detail": "PortAudio did not load", "critical": True}
    out = a.report(big)
    assert len(out) <= a.MAX_CHARS
    assert out.startswith('{"portaudio":{"ok":false,"detail":"PortAudio did not load"')
    assert json.loads(out)["check 3"] is True


def test_a_green_reports_details_are_split_across_notices_not_squeezed_out(tmp_path: Path) -> None:
    a = _annotate()
    big = {f"check {i}": {"ok": True, "detail": f"detail {i} " + "d" * 400} for i in range(20)}
    big["tray"] = {"ok": False, "detail": "pystray cannot run here", "critical": False}
    pieces = a.report_chunks(big)
    assert len(pieces) > 1
    assert all(len(p) <= a.MAX_CHARS for p in pieces)
    merged: dict[str, Any] = {}
    for p in pieces:
        merged.update(json.loads(p))
    assert merged == big, "every check, with its whole detail, in some notice"
    assert pieces[0].startswith('{"tray":'), "a failing check comes first"

    path = tmp_path / "selftest.json"
    path.write_text(json.dumps(big, indent=2), encoding="utf-8")
    lines = a.notice("selftest", path)
    assert len(lines) == len(pieces)
    assert lines[0].startswith(f"::notice title=selftest (1/{len(pieces)})::")
    assert a.notice("selftest", tmp_path / "absent.json")[0].startswith("::warning ")


def test_more_than_ten_logs_name_the_rest_instead_of_dropping_them(tmp_path: Path) -> None:
    a = _annotate()
    for i in range(14):
        (tmp_path / f"{i:02d}-step.log").write_text(f"output of step {i}\n", encoding="utf-8")
    out = a.failure(tmp_path)
    assert len(out) == a.MAX_PER_STEP
    assert all(line.startswith("::error title=") for line in out)
    assert "13-step.log" in out[-1] and "Not annotated" in out[-1]


def test_no_logs_at_all_is_itself_reported(tmp_path: Path) -> None:
    out = _annotate().failure(tmp_path)
    assert len(out) == 1 and "no logs were captured" in out[0]


def test_failed_tests_become_one_annotation_each(tmp_path: Path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="test_ok"/>'
        '<testcase classname="tests.test_a" name="test_path">'
        '<failure message="AssertionError: C:\\a">E   AssertionError: C:\\a</failure></testcase>'
        '<testcase classname="tests.test_b" name="test_boom"><error message="OSError">x</error>'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    out = _annotate().junit(report)
    assert len(out) == 2
    assert out[0].startswith("::error title=failure%3A tests.test_a.test_path::AssertionError")
    assert "tests.test_b.test_boom" in out[1]
    assert _annotate().junit(tmp_path / "absent.xml") == []


def test_the_annotator_needs_nothing_but_the_standard_library() -> None:
    """It runs when the install step is what failed."""
    tree = ast.parse(ANNOTATE.read_text(encoding="utf-8"))
    roots = {
        (alias.name if isinstance(node, ast.Import) else node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}


# ───────────────────────────── the icon ─────────────────────────────


def test_the_ico_is_a_valid_icon_with_every_size_windows_asks_for() -> None:
    data = ICO.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "not an ICO header"
    assert count >= 4
    sizes = set()
    for i in range(count):
        w, h, _colours, _res, _planes, _bpp, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * i
        )
        w, h = w or 256, h or 256  # 0 means 256 in an ICONDIRENTRY
        assert w == h
        assert offset + length <= len(data), f"the {w}px frame runs past the end of the file"
        frame = data[offset : offset + 8]
        assert frame.startswith(b"\x89PNG") or frame[:4] == struct.pack("<I", 40), (
            f"the {w}px frame is neither PNG nor a BITMAPINFOHEADER"
        )
        sizes.add(w)
    assert {16, 24, 32, 48, 256} <= sizes


def test_make_icon_draws_every_size_it_promises(tmp_path: Path) -> None:
    pytest.importorskip("PIL", reason='Pillow is the app extra: pip install -e ".[app]"')
    spec = importlib.util.spec_from_file_location("make_icon", ROOT / "packaging" / "make_icon.py")
    assert spec and spec.loader
    make_icon = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_icon)

    out = tmp_path / "x.ico"
    frames = make_icon.write_ico(out, sizes=(16, 32, 256))
    assert [f.size for f in frames] == [(256, 256), (32, 32), (16, 16)]
    assert all(f.mode == "RGBA" for f in frames)
    # Transparent corners: an icon is a disc, not a square.
    assert frames[0].getpixel((0, 0))[3] == 0
    assert out.read_bytes()[:4] == b"\x00\x00\x01\x00"


# ───────────────────────────── the app layer ─────────────────────────────


def test_the_app_layer_may_know_everybody_and_nobody_may_know_it() -> None:
    assert RULES["jarvis/app"] == ()
    for layer, forbidden in RULES.items():
        if layer != "jarvis/app":
            assert "jarvis.app" in forbidden, f"{layer} may import jarvis.app"


def test_a_spine_module_importing_the_app_is_reported(tmp_path: Path) -> None:
    for part in (layer for layer in RULES if layer != "spine"):
        (tmp_path / part).mkdir(parents=True, exist_ok=True)
        (tmp_path / part / "__init__.py").write_text("")
    (tmp_path / "jarvis" / "config.py").write_text("from jarvis.app import paths\n")
    (tmp_path / "jarvis" / "window" / "server.py").write_text(
        "def f():\n    from jarvis.app.setup import SetupService\n"
    )
    found = violations(tmp_path)
    assert any("jarvis/config.py" in line and "jarvis.app" in line for line in found)
    assert any("jarvis/window/server.py" in line and "jarvis.app" in line for line in found)


# ───────────────────────────── the docs ─────────────────────────────


def test_the_setup_guide_sends_a_windows_user_to_the_app_first() -> None:
    setup = (ROOT / "docs" / "setup.md").read_text(encoding="utf-8")
    head = "\n".join(setup.splitlines()[:15])
    assert "app.md" in head, "docs/setup.md must point at docs/app.md before anything else"
    app = (ROOT / "docs" / "app.md").read_text(encoding="utf-8")
    for must in ("Jarvis.exe", "More info", "Run anyway", "LOCALAPPDATA", "Jarvis-Windows-x64"):
        assert must in app, f"docs/app.md does not mention {must}"


def test_past_ten_failures_every_one_still_gets_its_diagnosis(tmp_path: Path) -> None:
    # A bare test name is not a diagnosis, and the run that failed thirty
    # tests on Windows is exactly the one that must say why for each of them.
    cases = "".join(
        f'<testcase classname="tests.test_w" name="test_{i}">'
        f'<failure message="AssertionError: case {i}">def test_{i}():\n'
        f">       assert f() == {i}\nE       AssertionError: wanted {i}\n\n"
        f"tests\\test_w.py:{i + 10}: AssertionError</failure></testcase>"
        for i in range(31)
    )
    report = tmp_path / "pytest.xml"
    report.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite>{cases}</testsuite></testsuites>',
        encoding="utf-8",
    )
    a = _annotate()
    out = a.junit(report)
    assert len(out) <= a.MAX_PER_STEP
    joined = "\n".join(out)
    for i in range(31):
        assert f"E       AssertionError: wanted {i}%0A" in joined or f"wanted {i}" in joined, i
    assert "Not annotated" not in joined
    assert all(len(line) < 4096 + 200 for line in out)
