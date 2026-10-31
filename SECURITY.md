# Security Policy

## Supported versions

Security fixes go into the latest release only. Upgrade to the newest version before you report a problem.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Report them privately, either through GitHub's [private vulnerability reporting](https://github.com/sergiovision/DevServer/security/advisories/new) or by email to **hi@sergego.com**. Please include:

- what the issue is, and its impact
- steps to reproduce it, or a proof of concept
- the affected version and deployment mode (host or Docker)

You will get an acknowledgement within 5 working days. Please give us reasonable time to release a fix before you disclose the issue publicly.

## Scope notes

DevServer runs AI coding-agent CLIs with write access to the repositories you configure, and is designed to run on a trusted host or network. These are especially relevant reports:

- a way around the PR preflight (secret, forbidden-file or author checks)
- path traversal (task logs, patch export)
- webhook signature bypass
- credentials leaking into logs, events or pull requests
