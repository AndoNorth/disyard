"""The service logins of the dispatch and control API, and the Secrets of its logins and store
identity.

The group roles of the app database cannot log in, so each group the service writes or reads as
gets a login in it: the submit sequence in `dsy_dispatch`, control in `dsy_control`, the index and
link rows in `dsy_artifacts` and the reads of event payloads in `dsy_reader`. Their passwords are
generated on first use into the state directory of the cluster scripts and never committed.
Usage: logins.py render STORE_CREDENTIALS | apply DSN | check DSN
`render` prints the two Secrets of the module, the DSNs of the logins and the `submit` store
identity read from the credentials file of the store, as a JSON list.
`apply` creates or converges each login as the superuser of DSN and prints one line per login with
what it did, ending with `changed: <n>`, and `check` exits 1 when a login differs or cannot log in.
"""

import sys

from dsy_clusterkit import logins as kit

# Each login and the one group role it is a member of, keyed by the environment variable of its DSN.
LOGINS = {
    "DSY_DISPATCH_DSN": ("dispatch_dispatch", "dsy_dispatch"),
    "DSY_CONTROL_DSN": ("dispatch_control", "dsy_control"),
    "DSY_ARTIFACTS_DSN": ("dispatch_artifacts", "dsy_artifacts"),
    "DSY_READER_DSN": ("dispatch_reader", "dsy_reader"),
}
NAMESPACE = "dsy-control"
STORE_ROLES = ("submit",)

spec = kit.Spec(LOGINS, NAMESPACE, "dispatch", STORE_ROLES)
MEMBER_OF, state_file, render, main = kit.MEMBER_OF, spec.state_file, spec.render, spec.run


if __name__ == "__main__":
    sys.exit(kit.cli(sys.argv[1:], spec))
