"""The service login of the state inputs exporter, and the Secret of its DSN.

The group role `dsy_reader` cannot log in, so the exporter gets the login `state_inputs` in it.
Its password is generated on first use into the state directory of the cluster scripts and never
committed.
Usage: logins.py render | apply DSN | check DSN
`render` prints the Secret of the module, the DSN of the login, as a JSON list.
`apply` creates or converges the login as the superuser of DSN and prints one line with what it
did, ending with `changed: <n>`, and `check` exits 1 when the login differs or cannot log in.
"""

import sys

from dsy_clusterkit import logins as kit

# Each login and the one group role it is a member of, keyed by the environment variable of its DSN.
LOGINS = {"DSY_STATE_INPUTS_DSN": ("state_inputs", "dsy_reader")}
NAMESPACE = "dsy-obs"

spec = kit.Spec(LOGINS, NAMESPACE, "state-inputs")
MEMBER_OF, state_file, render, main = kit.MEMBER_OF, spec.state_file, spec.render, spec.run


if __name__ == "__main__":
    sys.exit(kit.cli(sys.argv[1:], spec))
