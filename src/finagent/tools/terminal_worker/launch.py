"""Run real Python with the finance/mcp/emit helpers pre-injected."""
import runpy
import sys

from finance_runtime_sdk import finance, mcp, records, emit

runpy.run_path(sys.argv[1], run_name="__main__", init_globals={"finance": finance, "mcp": mcp, "records": records, "emit": emit})
