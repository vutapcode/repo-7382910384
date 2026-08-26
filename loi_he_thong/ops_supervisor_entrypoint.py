"""Canonical health-supervisor entrypoint."""

from loi_he_thong import ops_supervisor_canonical_cleanup as cleanup
from loi_he_thong import ops_supervisor_monotonic as monotonic

cleanup.install(monotonic.safe)


def main():
    monotonic.main()


if __name__ == "__main__":
    main()
