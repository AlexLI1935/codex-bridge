# Implementation provenance

This implementation was independently rewritten from behavioral requirements
and public Codex interfaces. The supplied legacy Skill was treated as a behavior
specification, not an implementation template. The reference package also
contained legacy source excerpts encountered during discovery; none was copied
into this project. The legacy package is not distributed here.

- Clean-room reimplementation under the project's no-copy requirements: PASS
- Old `real_integration.py` copied: NO
- Old third-party implementation copied: NO
- Customer-delivered source copied: NO
- CC Bridge implementation copied verbatim: NO
- Third-party code requiring attribution in the distributed source: none
- Runtime dependencies: Python standard library and an independently installed Codex CLI
- License: MIT, Copyright (c) 2026 Alex Li

Codex itself is not bundled or relicensed. Public interface sources and the
tested CLI version are recorded in [DISCOVERY.md](DISCOVERY.md).
