"""CI gate for the unit tests: read pytest's JUnit report and fail unless
every test passed and at least --min tests ran. A change that makes tests
skip or stop being collected cannot pass by running fewer of them.

    python -m pytest -q --junitxml=pytest.xml
    python tools/test_gate.py pytest.xml --min 110 --max-skipped 0
"""
import argparse
import os
import sys
import xml.etree.ElementTree as ET


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('report')
    ap.add_argument('--min', type=int, required=True, help='at least this many tests must run')
    ap.add_argument('--max-skipped', type=int, default=0)
    ap.add_argument('--label', default='Unit tests')
    a = ap.parse_args()
    root = ET.parse(a.report).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))
    total = sum(int(s.get('tests', 0)) for s in suites)
    failed = sum(int(s.get('failures', 0)) + int(s.get('errors', 0)) for s in suites)
    skipped = sum(int(s.get('skipped', 0)) for s in suites)
    passed = total - failed - skipped
    skipped_names = [f"{c.get('classname')}.{c.get('name')}" for c in root.iter('testcase')
                     if c.find('skipped') is not None]
    problems = []
    if failed:
        problems.append(f'{failed} failed')
    if total < a.min:
        problems.append(f'only {total} tests ran, at least {a.min} required')
    if skipped > a.max_skipped:
        problems.append(f'{skipped} skipped (at most {a.max_skipped}): ' + ', '.join(skipped_names[:10]))
    line = f'{a.label}: {total} ran, {passed} passed, {failed} failed, {skipped} skipped (minimum {a.min})'
    print(line)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write(f'### {a.label}\n\n| Ran | Passed | Failed | Skipped | Required at least |\n'
                    f'|---|---|---|---|---|\n| {total} | {passed} | {failed} | {skipped} | {a.min} |\n\n')
            for p in problems:
                f.write(f'- {p}\n')
    if problems:
        print('Gate failed: ' + '; '.join(problems))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
