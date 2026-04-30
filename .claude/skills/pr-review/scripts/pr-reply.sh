#!/usr/bin/env bash
set -euo pipefail

# Reply to a PR review comment, optionally resolve its thread.
# Usage: pr-reply.sh [--repo OWNER/REPO] [--resolve] PR_NUMBER COMMENT_ID "body"

REPO=""
RESOLVE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --resolve) RESOLVE=true; shift ;;
        *) break ;;
    esac
done

PR_NUMBER="${1:?Usage: pr-reply.sh [--repo OWNER/REPO] [--resolve] PR_NUMBER COMMENT_ID \"body\"}"
COMMENT_ID="${2:?Missing COMMENT_ID}"
BODY="${3:?Missing reply body}"

if [[ -z "$REPO" ]]; then
    REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
fi

# Append signature only if the body isn't already signed.
if ! printf '%s' "$BODY" | grep -qE '(^|\n)[[:space:]]*-[[:space:]]+Claude[[:space:]]*$'; then
    BODY="${BODY}

- Claude"
fi

# Post reply
REPLY_URL=$(gh api "repos/$REPO/pulls/$PR_NUMBER/comments/$COMMENT_ID/replies" \
    -f body="$BODY" \
    --jq '.html_url')
echo "Replied: $REPLY_URL"

# Resolve thread if requested
if [[ "$RESOLVE" == true ]]; then
    # Find the thread ID for this comment
    THREAD_ID=$(gh api graphql -f query="
    {
      repository(owner: \"${REPO%%/*}\", name: \"${REPO##*/}\") {
        pullRequest(number: $PR_NUMBER) {
          reviewThreads(first: 100) {
            nodes {
              id
              comments(first: 100) {
                nodes { databaseId }
              }
            }
          }
        }
      }
    }" --jq ".data.repository.pullRequest.reviewThreads.nodes[] | select(any(.comments.nodes[]; .databaseId == $COMMENT_ID)) | .id")

    if [[ -n "$THREAD_ID" ]]; then
        RESOLVED=$(gh api graphql -f query="
          mutation { resolveReviewThread(input: {threadId: \"$THREAD_ID\"}) { thread { isResolved } } }
        " --jq '.data.resolveReviewThread.thread.isResolved')
        echo "Resolved: $RESOLVED (thread $THREAD_ID)"
    else
        echo "Warning: could not find thread for comment $COMMENT_ID"
    fi
fi
