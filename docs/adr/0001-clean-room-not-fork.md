# ADR 0001 — Clean-room build, not a fork of Mark-LIII

**Status:** accepted, 16 Sep 2026
**Decided by:** Big Efk

## Context

The project began as "fork FatihMakes/Mark-LIII". That repo is licensed **CC BY-NC 4.0**, whose
NonCommercial term binds every derivative permanently. Copying even 200 lines makes this entire repository
non-commercial forever — no consulting engagement built on it, no sponsored video, no paid template, no
hosted version — without a separately negotiated licence, negotiated from the weak position of already
having the code in the tree.

The architectural case is decisive on its own. Mark-LIII's value is concentrated in `main.py`'s `JarvisLive`
(1,744 lines): one `self.session`, one `out_queue`, tools awaited inline in `session.receive()`, the UI object
handed to every action as `ctx["player"]`. Every one of those is an assumption this architecture exists to
refuse. A fork would spend week one deleting what it forked. The recon's own reuse verdict says REWRITE for
all three things Jarvis actually needs.

## Decision

Clean-room. MIT licence. A `CREDITS.md` naming Mark-LIII as acknowledged inspiration with a link — voluntary,
because it is decent, not because it is owed. Reading source and being influenced carries no licence
obligation; copying expression does.

`CONTRIBUTING.md` rule 1, one line: *nothing from the mark-liii checkout enters this tree; the reference file
is never open in the editor while the corresponding Jarvis file is being written.*

Two files are genuinely worth having and are still not copied. `core/audio_devices.py` (421 lines) documents
three failed device-probing designs in its comments — read it as a bug list and a specification, then
re-derive against `sounddevice`. This design needs a different probe anyway: it must assert capture and render
are the *same physical device*, which the reference never checks and which is the most common silent cause of
AEC failing quietly after two minutes. `core/wake_word.py` (202 lines) is a wrapper whose pretrained phrase is
literally `hey_jarvis`; openWakeWord's own quickstart gets you most of it in 30 lines.

## Escape clause

If a file is ever pasted in, that same day the repo gains `LICENSE` (CC BY-NC 4.0 verbatim), `NOTICE`,
`CHANGES.md` indicating modification, and a README line stating the repo is non-commercial forever. That
decision gets made once, deliberately, in writing — never accidentally by a `git add -A`.

## Consequence, acted on in commit 1

The reference's `.gitignore` secret patterns are silently inert. Confirmed mechanism: the lines carry trailing
same-line comments (`config/api_keys.json          # your Gemini API key`), and `.gitignore` has no
trailing-comment syntax — the whole line including the spaces and the `#` is one literal pattern that matches
nothing. `git check-ignore -v config/api_keys.json` returns empty; `git add -A` there publishes the key.

This repo's `.gitignore` keeps every comment on its own line, and CI runs `git check-ignore -v` against every
secret pattern and fails if any returns empty. The real fix is that no credential is in the tree at all —
they live in the OS keyring.
