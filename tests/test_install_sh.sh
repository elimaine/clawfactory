#!/usr/bin/env bash
#
# ClawFactory Install Script Tests
#
# Tests the install.sh script functions and options
# Run with: ./tests/test_install_sh.sh
#
# Note: These tests verify the script structure and syntax,
# not the actual installation (which would modify the system).
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
INSTALL_SH="${ROOT_DIR}/install.sh"

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
    if [[ -x "$INSTALL_SH" ]]; then
        pass "install.sh exists and is executable"
    else
        fail "install.sh exists and is executable" "File not found or not executable"
    fi
}

# ============================================================
# Test: Script has valid bash syntax
# ============================================================
test_bash_syntax() {
    run_test
    if bash -n "$INSTALL_SH" 2>/dev/null; then
        pass "install.sh has valid bash syntax"
    else
        fail "install.sh has valid bash syntax" "Syntax errors found"
    fi
}

# ============================================================
# Test: Required functions exist
# ============================================================
test_required_functions() {
    local functions=(
        "configure_secrets"
        "configure_ai_providers"
        "configure_vector_memory"
        "init_bot_repo"
        "preflight"
        "create_state_config"
    )

    for func in "${functions[@]}"; do
        run_test
        if grep -q "^${func}()" "$INSTALL_SH" || grep -q "^function ${func}" "$INSTALL_SH"; then
            pass "function ${func}() exists"
        else
            fail "function ${func}() exists" "Function not found in script"
        fi
    done
}

# ============================================================
# Test: Help flag works
# ============================================================
test_help_flag() {
    run_test
    local output
    output=$("$INSTALL_SH" --help 2>&1) || true
    if echo "$output" | grep -qi "usage\|help\|install"; then
        pass "--help flag shows usage info"
    else
        fail "--help flag shows usage info" "Did not find usage information"
    fi
}

# ============================================================
# Test: Script handles missing docker gracefully
# ============================================================
test_docker_check() {
    run_test
    if grep -q "docker" "$INSTALL_SH" && grep -q "command -v\|which" "$INSTALL_SH"; then
        pass "script checks for docker availability"
    else
        fail "script checks for docker availability" "No docker check found"
    fi
}

# ============================================================
# Test: GIT_USER variables are configured
# ============================================================
test_git_user_config() {
    run_test
    if grep -q "GIT_USER_NAME" "$INSTALL_SH" && grep -q "GIT_USER_EMAIL" "$INSTALL_SH"; then
        pass "GIT_USER_NAME and GIT_USER_EMAIL are configured"
    else
        fail "GIT_USER_NAME and GIT_USER_EMAIL are configured" "Variables not found"
    fi
}

# ============================================================
# Test: Snapshot key generation
# ============================================================
test_snapshot_key_generation() {
    run_test
    if grep -q "age-keygen" "$INSTALL_SH" && grep -q "snapshot.key" "$INSTALL_SH"; then
        pass "snapshot key generation is configured"
    else
        fail "snapshot key generation is configured" "age-keygen or snapshot.key not found"
    fi
}

# ============================================================
# Test: Token generation
# ============================================================
test_token_generation() {
    run_test
    if grep -q "openssl rand" "$INSTALL_SH" || grep -q "GATEWAY_TOKEN" "$INSTALL_SH"; then
        pass "token generation is configured"
    else
        fail "token generation is configured" "Token generation not found"
    fi
}

# ============================================================
# Test: Controller env has required vars
# ============================================================
test_controller_env_vars() {
    local vars=(
        "CONTROLLER_API_TOKEN"
        "OPENCLAW_GATEWAY_TOKEN"
        "INSTANCE_NAME"
        "GIT_USER_NAME"
        "GIT_USER_EMAIL"
    )

    for var in "${vars[@]}"; do
        run_test
        if grep -q "${var}" "$INSTALL_SH"; then
            pass "controller.env includes ${var}"
        else
            fail "controller.env includes ${var}" "Variable not found in script"
        fi
    done
}

# ============================================================
# Test: Offline mode support
# ============================================================
test_offline_mode() {
    run_test
    if grep -q "GH_AVAILABLE\|OFFLINE\|local.*mode" "$INSTALL_SH"; then
        pass "offline/local mode is supported"
    else
        fail "offline/local mode is supported" "No offline mode handling found"
    fi
}

# ============================================================
# Test: Use saved secrets option
# ============================================================
test_use_saved_secrets() {
    run_test
    if grep -q "USE_SAVED\|saved.*secret\|existing.*secret" "$INSTALL_SH"; then
        pass "use saved secrets option exists"
    else
        fail "use saved secrets option exists" "No saved secrets handling found"
    fi
}

# ============================================================
# Test: Vector memory configuration
# ============================================================
test_vector_memory_config() {
    run_test
    if grep -q "vector.*memory\|embedding\|nomic-embed" "$INSTALL_SH"; then
        pass "vector memory configuration exists"
    else
        fail "vector memory configuration exists" "No vector memory config found"
    fi
}

# ============================================================
# Main
# ============================================================
echo "ClawFactory Install Script Tests"
echo "================================="
echo ""

test_script_exists
test_bash_syntax
test_required_functions
test_help_flag
test_docker_check
test_git_user_config
test_snapshot_key_generation
test_token_generation
test_controller_env_vars
test_offline_mode
test_use_saved_secrets
test_vector_memory_config

echo ""
echo "================================="
echo "Results: ${TESTS_PASSED}/${TESTS_RUN} passed"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "${RED}${TESTS_FAILED} test(s) failed${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
