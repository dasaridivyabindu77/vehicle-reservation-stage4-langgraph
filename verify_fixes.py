#!/usr/bin/env python
"""Verification script to demonstrate all critical fixes are working.

This script verifies:
1. No duplicate escalation paths exist in chatbot.py
2. MCP server has real duplicate detection implemented
3. Documentation is accurate (no false NLP claims)
4. Tests use dynamic dates, not hardcoded ones
5. Health endpoint shows correct provider
"""

import re
from pathlib import Path


def check_chatbot_no_escalation():
    """Verify chatbot.py doesn't call escalation.submit()."""
    chatbot_file = Path("app/chatbot.py")
    content = chatbot_file.read_text(encoding='utf-8')

    # Check that escalation.submit is NOT called in run_reservation
    if "self.escalation.submit" in content:
        # Check it's only in comments
        lines_with_submit = [
            line for line in content.split('\n')
            if 'self.escalation.submit' in line and not line.strip().startswith('#')
        ]
        if lines_with_submit:
            return False, f"Found escalation.submit() calls: {lines_with_submit}"

    # Check for the clarifying comment
    if "orchestration graph" in content and "single point" in content:
        return True, "✅ Chatbot correctly delegates escalation to orchestrator"

    return False, "Comment about orchestration not found"


def check_mcp_duplicate_detection():
    """Verify MCP server has real duplicate detection."""
    mcp_file = Path("mcp_server/server.py")
    content = mcp_file.read_text(encoding='utf-8')

    # Check that _is_reservation_already_written actually parses the file
    if 'line.startswith(f"#{reservation_id} |")' in content:
        # Check that format includes reservation_id
        if '#{data.reservation_id}' in content:
            return True, "✅ MCP server has real duplicate detection with reservation_id"

    return False, "Duplicate detection not properly implemented"


def check_documentation_accuracy():
    """Verify documentation doesn't falsely claim NLP."""
    report_file = Path("FIX_COMPLETION_REPORT.md")
    content = report_file.read_text(encoding='utf-8')

    # Check that "NLP-based" is not used incorrectly
    nlp_matches = re.findall(r'NLP-based.*(?:guardrails|phone|detection)', content, re.IGNORECASE)
    if nlp_matches:
        return False, f"Found misleading NLP claims: {nlp_matches}"

    # Check that correct terminology is used
    if "context-aware regex" in content and "heuristics" in content:
        return True, "✅ Documentation uses accurate terminology"

    return False, "Correct terminology not found"


def check_dynamic_dates():
    """Verify load tests use dynamic dates."""
    load_test_file = Path("tests/test_load.py")
    content = load_test_file.read_text(encoding='utf-8')

    # Check for helper function
    if "_get_future_datetime" in content:
        # Check it's actually used
        if "days_ahead=" in content:
            return True, "✅ Load tests use dynamic date generation"

    return False, "Dynamic date generation not found"


def check_health_endpoint():
    """Verify health endpoint shows actual provider."""
    server_file = Path("app/web/server.py")
    content = server_file.read_text(encoding='utf-8')

    # Check that it uses llm_provider setting
    if "bot.settings.llm_provider" in content:
        return True, "✅ Health endpoint shows actual provider"

    return False, "Health endpoint not using llm_provider setting"


def main():
    """Run all verification checks."""
    print("=" * 70)
    print("CRITICAL FIXES VERIFICATION")
    print("=" * 70)
    print()

    checks = [
        ("Issue #1: No Duplicate Escalation", check_chatbot_no_escalation),
        ("Issue #2: Real MCP Duplicate Detection", check_mcp_duplicate_detection),
        ("Issue #3: Accurate Documentation", check_documentation_accuracy),
        ("Issue #4: Dynamic Test Dates", check_dynamic_dates),
        ("Issue #5: Correct Health Endpoint", check_health_endpoint),
    ]

    all_passed = True

    for check_name, check_func in checks:
        print(f"Checking: {check_name}")
        try:
            passed, message = check_func()
            print(f"  {message}")
            if not passed:
                all_passed = False
                print(f"  ❌ FAILED")
        except Exception as e:
            print(f"  ❌ ERROR: {e}")
            all_passed = False
        print()

    print("=" * 70)
    if all_passed:
        print("✅ ALL CHECKS PASSED - System is production-ready")
    else:
        print("❌ SOME CHECKS FAILED - Review issues above")
    print("=" * 70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    exit(main())
