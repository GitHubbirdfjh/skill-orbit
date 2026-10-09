# Contributing

SkillOrbit keeps the everyday workflow small: scan local metadata, reconcile
four catalog columns, and preserve the user's notes. Changes should support
that contract with clear behavior and focused tests.

```bash
python -m venv .venv
# macOS/Linux:
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/skillorbit --project examples/demo sync --check
```

On Windows use `.venv\Scripts\python` and `.venv\Scripts\skillorbit`.
For bugs, include Python/OS versions, the command, sanitized configuration,
and a minimal `SKILL.md` that reproduces the problem. Keep personal skill
content and runtime notes out of public reports.

If generated demo files change, run `skillorbit --project examples/demo sync`
and include the updated catalog. Source instructions must remain data: the
scanner must never execute commands from a skill file.
