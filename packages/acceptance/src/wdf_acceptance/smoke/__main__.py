"""`python -m wdf_acceptance.smoke`: the smoke gate on the cluster of WDF_PRESET (default `ci`), run
from the root of the tree by `scripts/smoke.sh`, which holds the cluster lock and the budget timer.

Prints one `ok|FAIL|skip <row> <detail>` line per row, then the count line. Exit 0 every row ok,
1 a row failed or the budget ran out, and 2 or 4 when `cluster-verify`'s usage or refusal exit
ended the run.
"""

import os
import sys
import traceback

from . import main

if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        traceback.print_exc()
        code = 1
    # The interpreter's teardown drops temporalio's runtime, which can park for good on a blocking
    # thread, and nothing is left to tear down once the run has printed, so the process ends here.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
