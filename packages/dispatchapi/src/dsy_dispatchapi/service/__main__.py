"""`python -m dsy_dispatchapi.service`: the dispatch and control API, configured from the
environment."""

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
