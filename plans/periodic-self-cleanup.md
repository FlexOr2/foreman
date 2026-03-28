# Periodic Self-Cleanup

Foreman should periodically review its own codebase for architectural debt and generate refactoring plans — just like the innovator generates feature plans, but focused on code quality.

## What to do

### 1. Add a cleanup cycle to the innovator loop

Use `architecture-review.md` in the repo root as the review template. The brain should follow its structure: analyze across all dimensions (structure, abstractions, data flow, error handling, testability, concurrency, etc.), produce a scorecard, and generate concrete refactoring plans from the findings.

After every N innovation cycles (configurable, default 3), run a cleanup cycle instead of a feature cycle. The brain reads `architecture-review.md` as its prompt template, applies it to the current codebase, and generates plans from The Bad and The Ugly sections. The cleanup cycle asks the brain:

- "Which files are over 200 lines and should be split?"
- "What functions do more than one thing?"
- "What code is duplicated across modules?"
- "What imports or functions are unused?"
- "What was added by a previous plan but is no longer needed?"
- "Does the code still match CLAUDE.md rules?"

The brain reads the codebase, identifies concrete refactoring tasks, and writes them as plans.

### 2. Add a test generation cycle

After every M innovation cycles (configurable, default 5), run a test cycle. The brain asks:

- "What core logic has no test coverage?"
- "What edge cases in the resolver/parser/coordination could break silently?"
- "What integration between modules is untested?"

Generates test plans that the implementation agent writes as pytest tests.

### 3. Update CLAUDE.md as part of cleanup

The cleanup cycle should also check if CLAUDE.md still reflects the actual codebase:
- Are the module descriptions accurate after refactors?
- Are there new conventions that emerged and should be documented?
- Are there rules that no longer apply?

If CLAUDE.md is outdated, generate a plan to update it. This keeps the review agent's architecture checks grounded in reality.

### 3. Config

```toml
[foreman.innovate]
cleanup_every = 3       # run cleanup cycle every 3 innovation cycles
test_every = 5          # run test generation cycle every 5 innovation cycles
```

### 4. Track cycle count

Add a cycle counter to the innovator loop. Persist it in `.foreman/innovator_state.json` so it survives restarts.

```python
cycle = load_cycle_count()
if cycle % config.innovate.cleanup_every == 0:
    await run_cleanup_cycle(config, brain)
elif cycle % config.innovate.test_every == 0:
    await run_test_cycle(config, brain)
else:
    await innovate(config)
save_cycle_count(cycle + 1)
```

## Files to modify

- `foreman/innovate.py` — add `run_cleanup_cycle`, `run_test_cycle`, cycle counting
- `foreman/config.py` — add `cleanup_every`, `test_every` to InnovateConfig
- `foreman/loop.py` — update `_innovator_loop` to use cycle counter
