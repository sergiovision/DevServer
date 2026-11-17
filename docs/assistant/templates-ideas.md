# Templates, ideas and schedules

## Templates (`/templates`)

A template is a saved task preset: description, acceptance criteria, task type,
git flow, vendor/model, backup model, billing mode, max turns and skip-verify.
Pick one from the dropdown on the new-task form and every field is filled in;
you then edit whatever differs.

Worth templating: release checklists, dependency bumps, "add tests for module
X", recurring audits — anything you type more than twice.

## Ideas (`/ideas`)

A hierarchical brainstorm tree. Nodes hold free-form notes and can be nested,
reordered, and expanded. Two things make it more than an outliner:

- **Decompose** — asks the system LLM to break a node into child nodes.
- **Roll up** — summarises a subtree back into its parent.

Any node can be converted into a task, carrying its text across. The workflow
is: dump thoughts → decompose → prune → promote the survivors to tasks.

**Generate plan** (the DevPlan skill) turns a project name plus a description
into a structured implementation plan with a plan key and a detailed prompt. If
`OBSIDIAN_FOLDER` is set, the plan is also written into your Obsidian vault as
markdown.

## Jobs (`/jobs`)

Cron schedules that re-run a task on an interval — nightly dependency checks,
weekly lint sweeps, a recurring health task. A job points at an existing task
and re-queues it on its schedule. You can run a job immediately or stop one
mid-flight from the Jobs page.

## Importing work

DevServer can import external work items and pages and turn them into tasks or
ideas — Confluence today, with the same pluggable importer designed to take
Azure DevOps and Jira. Configure the source per repository, search, preview,
then create. Re-importing the same page updates rather than duplicates.
