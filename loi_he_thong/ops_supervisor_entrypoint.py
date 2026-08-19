"""Canonical health-supervisor entrypoint."""

from loi_he_thong import ops_supervisor_canonical_cleanup as cleanup
from loi_he_thong import ops_supervisor_safe as safe

cleanup.install(safe)


def main():
    safe.main()


if __name__ == "__main__":
    main()
