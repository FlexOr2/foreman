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
        if [[ "${MOCK_MERGE_CONFLICT:-0}" == "1" ]]; then
            echo "feature content" > conflict.txt
            git add conflict.txt
            git commit -m "implement plan"
            MAIN_REPO="$(dirname "$(git rev-parse --git-common-dir)")"
            echo "main content" > "$MAIN_REPO/conflict.txt"
            git -C "$MAIN_REPO" add conflict.txt
            git -C "$MAIN_REPO" -c user.email="test@test.com" -c user.name="Test" commit -m "concurrent main change"
        else
            echo "implemented" > implementation.txt
            git add implementation.txt
            git commit -m "implement plan"
        fi
        ;;
    review)
        VERDICT="${MOCK_REVIEW_VERDICT:-clean}"
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
        git rebase main || {
            git checkout --theirs -- .
            git add -A
            GIT_EDITOR=: git rebase --continue
        }
        ;;
esac

exit 0
