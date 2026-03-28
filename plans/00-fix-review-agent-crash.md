# Fix Review Agent Crash — Exit Code 1 After ~20 Seconds

## Problem

Review agents crash with exit code 1 approximately 20 seconds after spawning. Implementation agents using the identical launcher script structure work fine. This blocks ALL reviews — no plan can get through the review cycle.

## Symptoms

- Implementation agent: spawns, works for 3-15 minutes, commits, exits 0 ✓
- Review agent: spawns, exits with code 1 after ~20 seconds ✗
- The launcher scripts for both are structurally identical (same flags, same trap)
- The only historical difference was `--allowed-tools` and `--bare` on review agents, but both were removed and the problem persists

## What we know

1. Running `claude -p "review prompt"` in print mode from the worktree works perfectly — produces a verdict
2. Running `claude` interactively in a manual tmux window works perfectly — interactive prompt appears
3. Running `bash launcher-script.sh` from a non-tmux shell produces: `Warning: no stdin data received in 3s` then `Error: Input must be provided either through stdin or as a prompt argument when using --print`
4. Claude CLI auto-detects `--print` mode when stdin is not a TTY
5. When tmux runs `new-window ... "bash script.sh"`, the bash process SHOULD get a pseudo-TTY from tmux

## The question

Why does claude see stdin as non-TTY inside the tmux window for review agents but NOT for implementation agents? Both use the same `_build_launcher_script` function and the same `tmux new-window` invocation.

## Things to investigate

- Is the foreman process spawning the review `tmux new-window` command differently than implementation?
- Does the `_wait_for_ready` + `send_text` timing differ between implementation and review spawns?
- Is there a race condition where the review window gets created but stdin isn't properly attached?
- Does the tmux window name length matter? Review windows have `__review` suffix
- Could the EXIT trap in the launcher script interfere with TTY allocation?
- Would adding `< /dev/tty` or using `script -c` to force a TTY help?
- Would running review agents in `-p` (print) mode with the initial message as argument be a viable workaround? Reviews don't need interactivity.

## Possible fixes (pick one)

A. Find and fix whatever breaks the TTY for review windows
B. Run review agents in `-p` mode (non-interactive, print result to stdout, parse verdict from output instead of file)
C. Run review agents via `asyncio.create_subprocess_exec` directly instead of tmux (they don't need user visibility)

## Files to investigate

- `foreman/spawner.py` — `_build_launcher_script`, `spawn_agent`, `TmuxBackend.create_terminal`
- `foreman/scheduler.py` — how review spawning differs from implementation spawning
- `foreman/loop.py` — the flow from implementation_done → spawn_review
