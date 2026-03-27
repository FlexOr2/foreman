# Review Cycle Bugs

The review/fix/done pipeline has several logic bugs that cause incorrect state transitions: agents treated as successful when they crashed, review retries exhausted prematurely, and pending reviews silently lost on error.

## Issues

### 1. Crashed agents treated as successful (High)

`_read_exit_code()` returns `0` (success) when the sentinel file is missing or contains non-integer content:
