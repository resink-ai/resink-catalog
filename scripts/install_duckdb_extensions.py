"""Install the DuckDB extensions the DuckDB tests need, and check they load. Run by the release CI before the tests
(the tests themselves never touch the network, D10). Exit status 1 if any extension cannot be installed or loaded.

    uv run python scripts/install_duckdb_extensions.py
"""

import sys

import duckdb

EXTENSIONS = ("httpfs", "iceberg")


def main() -> int:
    print("duckdb", duckdb.__version__)
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    for ext in EXTENSIONS:
        try:
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
        except duckdb.Error as e:
            print(f"FAILED {ext}: {e}", file=sys.stderr)
            return 1
        print(f"ok     {ext}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
