#!/usr/bin/env python3

#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# The following only applies to changes made to this file as part of YugaByte development.
#
# Portions Copyright (c) YugaByte, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
# in compliance with the License.  You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License
# is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied.  See the License for the specific language governing permissions and limitations
# under the License.
#
# This script parses a test log (provided on stdin) and returns
# a summary of the error which caused the test to fail.

import argparse
import os
import re
import sys

from xml.sax.saxutils import quoteattr

from typing import Iterable, List, Pattern, Match, Dict, Tuple, Optional, TextIO


# Read at most 100MB of a test log.
# Rarely would this be exceeded, but we don't want to end up
# swapping, etc.
MAX_MEMORY = 100 * 1024 * 1024

START_TESTCASE_RE = re.compile(r'\[ RUN\s+\] (.+)$')
END_TESTCASE_RE = re.compile(r'\[\s+(?:OK|FAILED)\s+\] (.+)$')
ASAN_ERROR_RE = re.compile('ERROR: AddressSanitizer')
TSAN_ERROR_RE = re.compile('WARNING: ThreadSanitizer.*')
END_TSAN_ERROR_RE = re.compile('SUMMARY: ThreadSanitizer.*')
FATAL_LOG_RE = re.compile(r'^F\d\d\d\d \d\d:\d\d:\d\d\.\d\d\d\d\d\d\s+\d+ (.*)')
LEAK_CHECK_SUMMARY_RE = re.compile('Leak check.*detected leaks')
LINE_RE = re.compile(r"^.*$", re.MULTILINE)
STACKTRACE_ELEM_RE = re.compile(r'^    @')
IGNORED_STACKTRACE_ELEM_RE = re.compile(
    r'(google::logging|google::LogMessage|\(unknown\)| testing::)')
TEST_FAILURE_RE = re.compile(r'.*\d+: Failure$')
GLOG_LINE_RE = re.compile(r'^[WIEF]\d\d\d\d \d\d:\d\d:\d\d')
UNRECOGNIZED_ERROR = "Unrecognized error type. Please see the error log for more information."


def consume_rest(line_iter: Iterable[Match]) -> List[str]:
    """ Consume and return the rest of the lines in the iterator. """
    return [line_match.group(0) for line_match in line_iter]


def consume_until(line_iter: Iterable[Match], end_re: Pattern) -> List[str]:
    """
    Consume and return lines from the iterator until one matches 'end_re'.
    The line matching 'end_re' will not be returned, but will be consumed.
    """
    ret: List[str] = []
    for line_match in line_iter:
        line = line_match.group(0)
        if end_re.search(line):
            break
        ret.append(line)
    return ret


def remove_glog_lines(lines: List[str]) -> List[str]:
    """ Remove any lines from the list of strings which appear to be GLog messages. """
    return [line for line in lines if not GLOG_LINE_RE.search(line)]


def record_error(errors: Dict[Optional[str], List[str]], name: Optional[str], error: str) -> None:
    errors.setdefault(name, []).append(error)


def extract_failures(log_text: str) -> Tuple[List[str], Dict[Optional[str], List[str]]]:
    cur_test_case = None
    tests_seen = set()
    tests_seen_in_order: List[str] = list()
    errors_by_test: Dict[Optional[str], List[str]] = dict()

    # Iterate over the lines, using finditer instead of .split()
    # so that we don't end up doubling memory usage.
    line_iter = LINE_RE.finditer(log_text)
    for match in line_iter:
        line = match.group(0)

        # Track the currently-running test case
        m = START_TESTCASE_RE.search(line)
        if m:
            cur_test_case = m.group(1)
            if cur_test_case not in tests_seen:
                tests_seen.add(cur_test_case)
                tests_seen_in_order.append(cur_test_case)

        m = END_TESTCASE_RE.search(line)
        if m:
            cur_test_case = None

        # Look for ASAN errors.
        m = ASAN_ERROR_RE.search(line)
        if m:
            error_signature = line + "\n"
            asan_lines = remove_glog_lines(consume_rest(line_iter))
            error_signature += "\n".join(asan_lines)
            record_error(errors_by_test, cur_test_case, error_signature)

        # Look for TSAN errors
        m = TSAN_ERROR_RE.search(line)
        if m:
            error_signature = m.group(0)
            error_signature += "\n".join(remove_glog_lines(
                consume_until(line_iter, END_TSAN_ERROR_RE)))
            record_error(errors_by_test, cur_test_case, error_signature)

        # Look for test failures
        # - slight micro-optimization to check for substring before running the regex
        if 'Failure' in line:
            m = TEST_FAILURE_RE.search(line)
            if m:
                error_signature = m.group(0) + "\n"
                error_signature += "\n".join(remove_glog_lines(
                    consume_until(line_iter, END_TESTCASE_RE)))
                record_error(errors_by_test, cur_test_case, error_signature)

        # Look for fatal log messages (including CHECK failures)
        # - slight micro-optimization to check for 'F' before running the regex
        if line and line[0] == 'F':
            m = FATAL_LOG_RE.search(line)
            if m:
                error_signature = m.group(1) + "\n"
                remaining_lines = consume_rest(line_iter)
                remaining_lines = [
                    line for line in remaining_lines
                    if STACKTRACE_ELEM_RE.search(line) and
                    not IGNORED_STACKTRACE_ELEM_RE.search(line)
                ]
                error_signature += "\n".join(remaining_lines)
                record_error(errors_by_test, cur_test_case, error_signature)

        # Look for leak check summary (comes at the end of a log, not part of a single test)
        m = LEAK_CHECK_SUMMARY_RE.search(line)
        if m:
            heapcheck_test_case = "tcmalloc.heapcheck"
            if heapcheck_test_case not in tests_seen:
                tests_seen.add(heapcheck_test_case)
                tests_seen_in_order.append(heapcheck_test_case)
            error_signature = "Memory leak\n"
            error_signature += line + "\n"
            error_signature += "\n".join(consume_rest(line_iter))
            record_error(errors_by_test, heapcheck_test_case, error_signature)

    # Sometimes we see crashes that the script doesn't know how to parse.
    # When that happens, we leave a generic message to be picked up by Jenkins.
    if cur_test_case and cur_test_case not in errors_by_test:
        record_error(errors_by_test, cur_test_case, UNRECOGNIZED_ERROR)

    return (tests_seen_in_order, errors_by_test)


# Return failure summary formatted as text.
def text_failure_summary(tests: List[str], errors_by_test: Dict[Optional[str], List[str]]) -> str:
    msg = ''
    for test_name in tests:
        if test_name not in errors_by_test:
            continue
        for error in errors_by_test[test_name]:
            if msg:
                msg += "\n"
            msg += "%s: %s\n" % (test_name, error)
    return msg


# Print failure summary based on desired output format.
# 'tests' is a list of all tests run (in order), not just the failed ones.
# This allows us to print the test results in the order they were run.
# 'errors_by_test' is a dict of lists, keyed by test name.
def print_failure_summary(
        tests: List[str],
        errors_by_test: Dict[Optional[str], List[str]],
        is_xml: bool) -> None:
    # Plain text dump.
    if not is_xml:
        sys.stdout.write(text_failure_summary(tests, errors_by_test))

    # Fake a JUnit report file.
    else:
        # Example format:
        """
        <testsuites>
          <testsuite name="ClientTest">
            <testcase name="TestReplicatedMultiTabletTableFailover" classname="ClientTest">
              <error message="Check failed: ABC != XYZ">
                <![CDATA[ ... stack trace ... ]]>
              </error>
            </testcase>
          </testsuite>
        </testsuites>
        """
        cur_test_suite = None
        print('<testsuites>')

        found_test_suites = False
        for test_name in tests:
            (test_suite, test_case) = test_name.split(".")

            # Test suite initialization or name change.
            if test_suite and test_suite != cur_test_suite:
                if cur_test_suite:
                    print('  </testsuite>')
                cur_test_suite = test_suite
                print('  <testsuite name="%s">' % cur_test_suite)
                found_test_suites = True

            # Print each test case.
            print('    <testcase name="%s" classname="%s">' % (test_case, cur_test_suite))
            if errors_by_test.get(test_name):
                errors = "\n\n".join(errors_by_test[test_name])
                first_line = re.sub("\n.*", '', errors)
                print('      <error message=%s>' % quoteattr(first_line))
                print('<![CDATA[')
                print(errors)
                print(']]>')
                print('      </error>')
            print('    </testcase>')

        if found_test_suites:
            print('  </testsuite>')
        print('</testsuites>')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-x", "--xml",
        help="Print output in JUnit report XML format (default: plain text)",
        action="store_true")
    parser.add_argument(
        "junit_test_case_id",
        nargs="?",
        help="Test case ID in <Test suite>.<test case> format to be used for report in case it "
             "couldn't be parsed from log")
    parser.add_argument(
        "path",
        nargs="?",
        help="File to parse. If not provided, parses stdin")
    args = parser.parse_args()

    test_case_id = args.junit_test_case_id

    in_file: Optional[TextIO]
    if args.path:
        in_file = open(args.path) if os.path.isfile(args.path) else None
    else:
        in_file = sys.stdin

    tests: List[str]
    errors_by_test: Dict[Optional[str], List[str]]

    if in_file:
        log_text = in_file.read(MAX_MEMORY)
        (tests, errors_by_test) = extract_failures(log_text)
    else:
        tests = [test_case_id]
        errors_by_test = {test_case_id: ['Log file not found, check global build log']}

    # For non-gtest test we need to generate XML without errors.
    # Errors will be added by update_test_result_xml.py if needed.
    if len(tests) == 0:
        tests.append(test_case_id)
        errors_by_test[test_case_id] = []
    print_failure_summary(tests, errors_by_test, args.xml)


if __name__ == "__main__":
    main()
