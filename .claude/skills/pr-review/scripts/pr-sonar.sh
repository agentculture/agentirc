#!/usr/bin/env bash
# pr-sonar.sh — list every SonarCloud finding attached to a PR.
#
# Surfaces what the GitHub-side `poll` cannot: SonarCloud issues,
# security hotspots, and the duplication breakdown. The
# sonarqubecloud[bot] PR comment only links to the dashboard — actual
# findings live behind the SonarCloud API.
#
# Usage: pr-sonar.sh [--repo OWNER/REPO] [--sonar-key KEY] PR_NUMBER
#
# Defaults:
#   --repo        auto-detected via `gh repo view`
#   --sonar-key   derived from repo as `<owner>_<name>`
#
# Anonymous SonarCloud access is sufficient for public projects.
# Requires: gh, jq, curl, python3.

set -euo pipefail

REPO=""
SONAR_KEY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --sonar-key) SONAR_KEY="$2"; shift 2 ;;
        *) break ;;
    esac
done

PR_NUMBER="${1:?Usage: pr-sonar.sh [--repo OWNER/REPO] [--sonar-key KEY] PR_NUMBER}"

if [[ -z "$REPO" ]]; then
    REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
fi
if [[ -z "$SONAR_KEY" ]]; then
    SONAR_KEY="${REPO%%/*}_${REPO##*/}"
fi

echo "════════════════ SONARCLOUD (project=$SONAR_KEY, PR=$PR_NUMBER) ════════════════"

# ── Quality gate
QG=$(curl -fsS "https://sonarcloud.io/api/qualitygates/project_status?projectKey=${SONAR_KEY}&pullRequest=${PR_NUMBER}" || echo '{}')
QG_STATUS=$(echo "$QG" | jq -r '.projectStatus.status // "UNKNOWN"')
echo "Quality Gate: $QG_STATUS"
echo "$QG" | jq -r '.projectStatus.conditions[]? | select(.status != "OK") | "  ✗ \(.metricKey) = \(.actualValue) (threshold \(.comparator) \(.errorThreshold))"' || true

# ── Issues (BUG / VULNERABILITY / CODE_SMELL)
ISSUES=$(curl -fsS "https://sonarcloud.io/api/issues/search?componentKeys=${SONAR_KEY}&pullRequest=${PR_NUMBER}&statuses=OPEN,CONFIRMED&ps=500" || echo '{}')
ISSUE_TOTAL=$(echo "$ISSUES" | jq -r '.total // 0')
echo
echo "── Issues ($ISSUE_TOTAL OPEN/CONFIRMED) ─────────────────────────────"
if [[ "$ISSUE_TOTAL" != "0" ]]; then
    echo "$ISSUES" | jq -r '
        .issues[] |
        "  [\(.rule)] \(.severity)  \(.component | sub("^[^:]+:"; ""))(:\(.line // "?"))\n     \(.message)"
    '
fi

# ── Security hotspots
HOTSPOTS=$(curl -fsS "https://sonarcloud.io/api/hotspots/search?projectKey=${SONAR_KEY}&pullRequest=${PR_NUMBER}&status=TO_REVIEW&ps=500" || echo '{}')
HOTSPOT_TOTAL=$(echo "$HOTSPOTS" | jq -r '.paging.total // 0')
echo
echo "── Hotspots ($HOTSPOT_TOTAL TO_REVIEW) ──────────────────────────────"
if [[ "$HOTSPOT_TOTAL" != "0" ]]; then
    echo "$HOTSPOTS" | jq -r '
        .hotspots[] |
        "  [\(.ruleKey)] \(.vulnerabilityProbability)  \(.component | sub("^[^:]+:"; ""))(:\(.line // "?"))\n     \(.message)"
    '
fi

# ── Duplication
MEAS=$(curl -fsS "https://sonarcloud.io/api/measures/component?component=${SONAR_KEY}&pullRequest=${PR_NUMBER}&metricKeys=new_duplicated_lines_density,new_duplicated_lines,new_duplicated_blocks" || echo '{}')
DUP_PCT=$(echo "$MEAS" | jq -r '.component.measures[]? | select(.metric == "new_duplicated_lines_density") | .periods[0].value // ""')
DUP_LINES=$(echo "$MEAS" | jq -r '.component.measures[]? | select(.metric == "new_duplicated_lines") | .periods[0].value // ""')
DUP_BLOCKS=$(echo "$MEAS" | jq -r '.component.measures[]? | select(.metric == "new_duplicated_blocks") | .periods[0].value // ""')
echo
echo "── Duplication on new code ──────────────────────────────────────────"
echo "  density: ${DUP_PCT:-?}%   lines: ${DUP_LINES:-?}   blocks: ${DUP_BLOCKS:-?}"

if [[ "${DUP_BLOCKS:-0}" != "0" && -n "${DUP_BLOCKS:-}" ]]; then
    # Per-file duplication: list every file with duplicated_lines on new code.
    # Sonar doesn't expose per-PR duplication-by-file directly; surface the
    # files that appear in the issues + hotspots as a weak proxy.
    DUP_FILES=$(echo "$ISSUES" | jq -r '[.issues[] | .component | sub("^[^:]+:"; "")] | unique | .[]')
    if [[ -n "$DUP_FILES" ]]; then
        echo "  files with findings (likely duplication source):"
        echo "$DUP_FILES" | sed 's/^/    - /'
    fi
fi

echo
echo "Dashboard: https://sonarcloud.io/dashboard?id=${SONAR_KEY}&pullRequest=${PR_NUMBER}"
