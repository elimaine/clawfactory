#!/usr/bin/env bash
#
# ClawFactory CLI Tests
#
# Tests the clawfactory.sh script commands
# Run with: ./tests/test_clawfactory_sh.sh
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CLAWFACTORY="${ROOT_DIR}/clawfactory.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    echo -e "${GREEN}✓${NC} $1"
    TESTS_PASSED=$((TESTS_PASSED + 1))
}

fail() {
    echo -e "${RED}✗${NC} $1"
    echo "  Error: $2"
    TESTS_FAILED=$((TESTS_FAILED + 1))
}

skip() {
    echo -e "${YELLOW}⊘${NC} $1 (skipped)"
}

run_test() {
    TESTS_RUN=$((TESTS_RUN + 1))
}

# ============================================================
# Test: Script exists and is executable
# ============================================================
test_script_exists() {
    run_test
    if [[ -x "$CLAWFACTORY" ]]; then
        pass "clawfactory.sh exists and is executable"
    else
        fail "clawfactory.sh exists and is executable" "File not found or not executable"
    fi
}

# ============================================================
# Test: Help command works
# ============================================================
test_help_command() {
    run_test
    local output
    output=$("$CLAWFACTORY" help 2>&1) || true
    if echo "$output" | grep -q "Usage:"; then
        pass "help command shows usage"
    else
        fail "help command shows usage" "Did not find 'Usage:' in output"
    fi
}

# ============================================================
# Test: Unknown command shows help
# ============================================================
test_unknown_command() {
    run_test
    local output
    output=$("$CLAWFACTORY" unknowncommand 2>&1) || true
    if echo "$output" | grep -q "Usage:"; then
        pass "unknown command shows help"
    else
        fail "unknown command shows help" "Did not show help for unknown command"
    fi
}

# ============================================================
# Test: Instance flag parsing
# ============================================================
test_instance_flag() {
    run_test
    local output
    # This should fail but show the instance name
    output=$("$CLAWFACTORY" -i myinstance info 2>&1) || true
    if echo "$output" | grep -q "myinstance"; then
        pass "instance flag (-i) is parsed"
    else
        fail "instance flag (-i) is parsed" "Instance name not found in output"
    fi
}

# ============================================================
# Test: List command works
# ============================================================
test_list_command() {
    run_test
    local output
    output=$("$CLAWFACTORY" list 2>&1) || true
    if echo "$output" | grep -qi "instance\|container"; then
        pass "list command works"
    else
        fail "list command works" "Did not find expected output"
    fi
}

# ============================================================
# Test: Status command (requires running instance)
# ============================================================
test_status_command() {
    run_test
    # Check if testbot is running
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "clawfactory-testbot"; then
        local output
        output=$("$CLAWFACTORY" -i testbot status 2>&1) || true
        if echo "$output" | grep -q "clawfactory"; then
            pass "status command shows containers"
        else
            fail "status command shows containers" "Did not find container info"
        fi
    else
        skip "status command (testbot not running)"
    fi
}

# ============================================================
# Test: Info command
# ============================================================
test_info_command() {
    run_test
    local output
    output=$("$CLAWFACTORY" -i testbot info 2>&1) || true
    if echo "$output" | grep -q "Instance:"; then
        pass "info command shows instance info"
    else
        fail "info command shows instance info" "Did not find 'Instance:' in output"
    fi
}

# ============================================================
# Test: Rebuild command exists
# ============================================================
test_rebuild_in_help() {
    run_test
    local output
    output=$("$CLAWFACTORY" help 2>&1) || true
    if echo "$output" | grep -q "rebuild"; then
        pass "rebuild command is listed in help"
    else
        fail "rebuild command is listed in help" "rebuild not found in help output"
    fi
}

# ============================================================
# Test: Controller URL command
# ============================================================
test_controller_command() {
    run_test
    local output
    output=$("$CLAWFACTORY" -i testbot controller 2>&1) || true
    if echo "$output" | grep -q "http"; then
        pass "controller command shows URL"
    else
        fail "controller command shows URL" "Did not find URL in output"
    fi
}

# ============================================================
# Main
# ============================================================
echo "ClawFactory CLI Tests"
echo "====================="
echo ""

test_script_exists
test_help_command
test_unknown_command
test_instance_flag
test_list_command
test_status_command
test_info_command
test_rebuild_in_help
test_controller_command

echo ""
echo "====================="
echo "Results: ${TESTS_PASSED}/${TESTS_RUN} passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}${TESTS_FAILED} test(s) failed${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
