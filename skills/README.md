# Skills

Drop one folder per skill here, each containing a `SKILL.md`. Skills are
**defined per deployment** — none ship by default. Run "Sync skills" on the
`/jobs` page (or `POST /api/skills`) to register what's on disk into the DB,
after which they appear in the schedule/skill pickers.

`SKILL.md` format (YAML-ish frontmatter + Markdown body):

```markdown
---
name: my-skill
description: One line — used for progressive disclosure and the picker.
domain: <optional: matches a project domain, or omit for generic>
version: 1
---

<Markdown instructions the agent follows when this skill is injected.>
```

Override the location with the `SKILLS_DIR` env var.
