#!/usr/bin/env bash
#
# ClawFactory Test Runner
#
# Runs all test suites and reports results.
# Usage: ./tests/run_tests.sh [--api] [--cli] [--install] [--all]
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SUITES_RUN=0
SUITES_PASSED=0
SUITES_FAILED=0

# Default: run all tests
RUN_API=false
RUN_CLI=false
RUN_INSTALL=false
RUN_ALL=true

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --api)
            RUN_API=true
            RUN_ALL=false
            shift
            ;;
        --cli)
            RUN_CLI=true
            RUN_ALL=false
            shift
            ;;
        --install)
            RUN_INSTALL=true
            RUN_ALL=false
            shift
            ;;
        --all)
            RUN_ALL=true
            shift
            ;;
        --help|-h)
            echo "ClawFactory Test Runner"
            echo ""
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --api      Run Controller API tests (requires running instance)"
            echo "  --cli      Run clawfactory.sh CLI tests"
            echo "  --install  Run install.sh structure tests"
            echo "  --all      Run all tests (default)"
            echo "  --help     Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

if [[ "$RUN_ALL" == "true" ]]; then
    RUN_API=true
    RUN_CLI=true
    RUN_INSTALL=true
fi

run_suite() {
    local name="$1"
    local cmd="$2"

    ((SUITES_RUN++))
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}Running: ${name}${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    if eval "$cmd"; then
        ((SUITES_PASSED++))
        echo ""
        echo -e "${GREEN}✓ ${name} PASSED${NC}"
    else
        ((SUITES_FAILED++))
        echo ""
        echo -e "${RED}✗ ${name} FAILED${NC}"
    fi
}

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║          ClawFactory Test Suite                              ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"

# Run selected test suites
if [[ "$RUN_INSTALL" == "true" ]]; then
    run_suite "Install Script Tests" "bash ${SCRIPT_DIR}/test_install_sh.sh"
fi

if [[ "$RUN_CLI" == "true" ]]; then
    run_suite "CLI Tests" "bash ${SCRIPT_DIR}/test_clawfactory_sh.sh"
fi

if [[ "$RUN_API" == "true" ]]; then
    # Check if pytest is available
    if command -v pytest &>/dev/null || command -v python3 &>/dev/null; then
        # Check if testbot is running
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "clawfactory-testbot-controller"; then
            run_suite "Controller API Tests" "python3 -m pytest ${SCRIPT_DIR}/test_controller_api.py -v --tb=short"
        else
            echo ""
            echo -e "${YELLOW}⚠ Skipping API tests: testbot controller not running${NC}"
            echo "  Start with: ./clawfactory.sh -i testbot start"
        fi
    else
        echo ""
        echo -e "${YELLOW}⚠ Skipping API tests: pytest not installed${NC}"
        echo "  Install with: pip install pytest requests"
    fi
fi

# Summary
echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}Test Summary${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Suites run:    $SUITES_RUN"
echo "Suites passed: $SUITES_PASSED"
echo "Suites failed: $SUITES_FAILED"
echo ""

if [[ $SUITES_FAILED -gt 0 ]]; then
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
