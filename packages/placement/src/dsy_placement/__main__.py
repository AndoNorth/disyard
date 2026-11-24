"""`python -m dsy_placement`: the placement service, configured from the environment. Exit 2 when
the configuration is refused."""

import sys

from .service import main

if __name__ == "__main__":
    sys.exit(main())
