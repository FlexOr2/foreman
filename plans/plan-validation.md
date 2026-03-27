# Dependency Validation and Plan Safety

Plan parsing silently ignores invalid dependencies and doesn't validate plan names, which can cause plans to run prematurely or create worktrees outside the intended directory.

## Issues

### 1. Unresolved dependencies silently ignored (High)

`validate_dag()` in `resolver.py` filters dependencies to only those matching known plan names:
