# Contributing

This is a personal project, but the rules below are load-bearing rather than ceremonial.

## Rule 1 — nothing from Mark-LIII enters this tree

This repository is MIT and clean-room. The reference build that inspired it
([FatihMakes/Mark-LIII](https://github.com/FatihMakes/Mark-LIII)) is CC BY-NC 4.0, whose NonCommercial
term binds **every derivative, permanently**. Copying even a couple of hundred lines would make this whole
tree non-commercial forever, and you would be negotiating a separate licence from a weak position — with
the code already in your history.

So: **the reference file is never open in the editor while the corresponding Jarvis file is being written.**

Reading source and being influenced by it carries no licence obligation. Copying expression does. Read a
file, understand what it does, close it, then write your own. `tools/check_no_reference_code.py` runs in CI
as a smoke alarm — it is not a proof, and the rule is the real control.

**If you ever decide to take the dependency deliberately**, do it in one commit that also adds `LICENSE`
(CC BY-NC 4.0 verbatim), `NOTICE`, a `CHANGES.md` indicating modification, and a README line stating the
repo is non-commercial forever. See [ADR 0001](docs/adr/0001-clean-room-not-fork.md).

## Rule 2 — no secret ever enters the tree

Credentials live in the OS keyring. Not in a `.env`, not in a JSON file, not in a test fixture.

`tools/check_gitignore.py` asserts against real `git check-ignore` that every secret pattern actually
fires. It exists because the reference build's `.gitignore` has five secret patterns that all match
nothing — each has a trailing same-line comment, and `.gitignore` has no trailing-comment syntax. Keep
every comment on its own line.

## Rule 3 — assume the reader is another process, started after you died

Every process opens the same SQLite file. Nothing important may live only in memory:

- Functions take an open `sqlite3.Connection` as their first argument. They never open one themselves.
- No module-level mutable state. No singletons.
- Use `jarvis.db.tx()` when something must be atomic; autocommit otherwise. Never leave a transaction open.
- If two processes could collide, there is a test that opens **two real connections** and proves who wins.

## Rule 4 — the spine imports on a bare interpreter

`jarvis/` core modules depend on the standard library only. The voice layer, the Claude Code driver and
the phone worker each add their own dependencies behind an extra. CI enforces this with `python -S`.

## Rule 5 — comments say why, never what

A comment restating the code is worse than none. Comment the race, the footgun, the decision that looks
wrong but is not. If you worked something out the hard way, write down what you learned so the next person
does not have to.

## Running things

```bash
uv venv && . .venv/bin/activate
uv pip install -e '.[cc,dev]'

pytest -q
ruff check . && ruff format --check .
python tools/check_gitignore.py
```

The spike that gates the whole design, re-runnable at any time:

```bash
python spikes/s0_crossproc/run_spike.py --gap 180
```
