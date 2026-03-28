#!/bin/bash
# Mock Claude CLI for integration tests.
# Detects agent type from the --name flag and performs the appropriate action.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) AGENT_NAME="$2"; shift 2 ;;
        *) shift ;;
    esac
done

AGENT_TYPE="${AGENT_NAME##*:}"

if [[ "${MOCK_CLAUDE_EXIT_CODE:-0}" != "0" ]]; then
    exit "${MOCK_CLAUDE_EXIT_CODE}"
fi

case "$AGENT_TYPE" in
    implementation)
        echo "implemented" > implementation.txt
        git add implementation.txt
        git commit -m "implement plan"
        ;;
    review)
        VERDICT="${MOCK_REVIEW_VERDICT:-clean}"
        # If a fix agent already ran, always approve.
        if [[ -f fix.txt ]]; then
            VERDICT="clean"
        fi
        printf '{"verdict": "%s", "issues": ["issue1"]}' "$VERDICT" > REVIEW_VERDICT.json
        ;;
    fix)
        echo "fixed" > fix.txt
        git add fix.txt
        git commit -m "fix review findings"
        ;;
    rebase)
        git rebase origin/main
        ;;
esac

exit 0
