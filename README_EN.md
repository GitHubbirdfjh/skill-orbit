<p align="center"><img src="docs/hero.svg" alt="SkillOrbit: Your skills. One living map." width="100%"></p>
<p align="center"><strong>A living Markdown directory for your project's AI skills.</strong></p>
<p align="center"><a href="README.md">中文</a> · English · <a href="LICENSE">MIT</a> · Python 3.10+</p>

SkillOrbit keeps a four-column inventory of local `SKILL.md` files: **name, project, one-sentence description, remarks**. Add, remove, or update a skill and the catalog follows. Your handwritten remarks survive synchronization and return when a removed skill is reinstalled under the same name and project.

## Start in one project

```bash
python3 -m venv ~/.venvs/skillorbit
~/.venvs/skillorbit/bin/python -m pip install "git+https://github.com/GitHubbirdfjh/skill-orbit.git"
cd /path/to/your-project
~/.venvs/skillorbit/bin/skillorbit init
~/.venvs/skillorbit/bin/skillorbit start
```

Without Git, download the repository ZIP, extract it, and run `python -m pip install .` in the extracted directory. If using pipx, install the GitHub URL with `pipx install`. Below, `skillorbit` assumes the executable is on PATH.

By default, it scans `.agents/skills`, writes `SKILLS.md`, and polls every two seconds. No API keys or AI service are needed; PyYAML is the only runtime dependency. Descriptions retain the source language.

## Useful commands

| Command | Purpose |
|---|---|
| `skillorbit init` | Create project configuration and the first catalog. |
| `skillorbit sync` | Reconcile once. |
| `skillorbit watch` | Run in the foreground. |
| `skillorbit start` / `stop` | Start or stop the background watcher. |
| `skillorbit status` | Check background health. |
| `skillorbit list QUERY` | Search name, project, and description. |
| `skillorbit note NAME "remark"` | Edit a remark; use `--owner` for ambiguous names. |
| `skillorbit sync --check` | Read-only freshness check: exit 0 current, 1 stale, 2 error. |
| `skillorbit service install` / `uninstall` | macOS login startup. |

The global option `--project PATH` must precede the command. For example: `skillorbit --project /path/to/project sync`.

## Keep existing work

```bash
skillorbit init --import-md catalog.md --output catalog.md
```

The old catalog is backed up before an in-place import. Columns must be name, project, description, and optionally remarks; skill names must use backticks. Imported descriptions remain until the source description changes. Same-project duplicates share a row with all their source files linked below the table. Different projects remain distinct.

## Assign ownership explicitly

Edit `.skillorbit.json`:

```json
{
  "version": 1,
  "roots": [".agents/skills"],
  "output": "SKILLS.md",
  "interval": 2,
  "project_rules": [
    {"match": ".agents/skills/research-kit/*", "project": "Research Kit"}
  ],
  "descriptions": {}
}
```

First matching rule wins. Otherwise, ownership comes from `project`, `metadata.project`, `metadata.upstream_suite`, or the nearest local Git origin. Unknown ownership is shown explicitly as Unassigned. Descriptions use the first source sentence, truncated at 180 characters. A permanent manual description can be configured as `"descriptions": {"skill-name": "Your concise description."}`.

Edit remarks directly in Markdown. Text outside the managed markers is retained; generated metadata inside them is refreshed. Malformed inputs preserve the last good catalog. Out-of-project symlinks are skipped. Writes are atomic and serialized with an OS lock.

## Runtime and persistence

`start` runs during the current login session. For macOS restart persistence, run `service install` from a stable Python environment. Linux and Windows can run `watch` through an existing user service or Task Scheduler. The scanner and background watcher support all three systems; CI is configured for each, and the actual results are visible in GitHub Actions.

Keep `.skillorbit.json` and the catalog in Git; ignore `.skillorbit/`, which stores notes for removed skills, the last inventory, recent changes, process state, and local logs. The tool never executes skill instructions and makes no network requests.

See [the demo catalog](examples/demo/SKILLS.md) and [configuration reference](docs/configuration.md).

## Develop

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
skillorbit --project examples/demo sync --check
```

Licensed under [MIT](LICENSE).
