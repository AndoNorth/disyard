"""Fails a PR title that release_notes.py would not file under its commit type.

The title becomes the squash commit subject on main, so it must follow
`type(scope)!: [#issue] summary`, see docs/delivery/release-process.md.
Usage: check_pr_title.py <title>
"""

import sys

from release_notes import check_title


def main():
    if len(sys.argv) != 2:
        print("usage: check_pr_title.py <title>", file=sys.stderr)
        return 2
    title = sys.argv[1]
    problems = check_title(title)
    if not problems:
        print(f"ok: {title}")
        return 0
    print(f"error: the PR title {title!r} does not follow the commit format:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("format: type(scope)!: [#issue] summary", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
