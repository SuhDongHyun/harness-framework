---
name: harness-setup
description: Prepare and verify the Personal Codex Harness runtime on Linux or WSL. Use when installing this repository, when Harness doctor reports runtime-home or login failures, when a user asks to configure Harness permissions, or before the first harness-managed run.
---

# Harness Setup

1. Read `AGENTS.md` and the Linux/WSL runtime boundary in `HARNESS_DESIGN.md`.
2. Run `python3 scripts/harness.py doctor`. If every check passes, report that setup is complete and stop.
3. Run `python3 scripts/harness.py setup` and parse its JSON. If the outer sandbox cannot create the printed runtime directory, request scoped escalation for this exact command only. Never escalate `plan`, `approve`, `run`, `review`, or the Python controller as a whole.
4. Check authentication with the printed runtime home: `env CODEX_HOME=<codex_home> codex login status`. If it is not logged in, launch `env CODEX_HOME=<codex_home> codex login` in a managed interactive terminal. Request scoped escalation for that exact login command when required. Wait for the login process; ask the user only to complete the browser authentication when Codex requires it. Never copy another Codex home's `auth.json`.
5. Resolve the outer Codex config as `$CODEX_HOME/config.toml`, falling back to `$HOME/.codex/config.toml` only when `CODEX_HOME` is unset. It must not be the dedicated Harness home.
6. Run `python3 .agents/skills/harness-setup/scripts/merge_writable_root.py --config <outer-config> --root <codex_home>`. The script atomically merges the runtime path into the outer config without replacing unrelated settings. If the outer sandbox blocks the config write, request scoped escalation for this exact script command only.
7. Run `python3 scripts/harness.py doctor` normally. If all checks pass, report completion. If only the active-sandbox writability check fails after the config changed, explain that Codex must be fully restarted because a running sandbox cannot gain a new writable root; do not escalate doctor to hide that result. Tell the user to restart Codex and invoke `$harness-setup` once more, or proceed with `$harness-plan`, whose preflight will confirm the new session.
8. Stop and report any other failed check with its exact detail. Do not edit project code or create a Harness run.
