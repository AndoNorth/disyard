"""The service logins of the workflow worker, and the Secrets of its logins and store identities.

The group roles of the app database cannot log in, so each writer the service runs gets a login in
its group: `emit` in `dsy_workflow`, the artifact activities in `dsy_artifacts` and the reaper in
`dsy_reaper`. Their passwords are generated on first use into the state directory of the cluster
scripts and never committed.
Usage: logins.py render STORE_CREDENTIALS | apply DSN | check DSN
`render` prints the two Secrets of the module, the DSNs of the logins and the store identities of
the service read from the credentials file of the store, as a JSON list.
`apply` creates or converges each login as the superuser of DSN and prints one line per login with
what it did, ending with `changed: <n>`, and `check` exits 1 when a login differs or cannot log in.
"""

import sys

from dsy_clusterkit import logins as kit

# Each login and the one group role it is a member of, keyed by the environment variable of its DSN.
LOGINS = {
    "DSY_WORKFLOW_DSN": ("workflow_worker_workflow", "dsy_workflow"),
    "DSY_ARTIFACTS_DSN": ("workflow_worker_artifacts", "dsy_artifacts"),
    "DSY_REAPER_DSN": ("workflow_worker_reaper", "dsy_reaper"),
}
NAMESPACE = "dsy-control"
STORE_ROLES = ("workflow", "indexer", "reaper")

spec = kit.Spec(LOGINS, NAMESPACE, "workflow-worker", STORE_ROLES)
MEMBER_OF, state_file, render, main = kit.MEMBER_OF, spec.state_file, spec.render, spec.run


if __name__ == "__main__":
    sys.exit(kit.cli(sys.argv[1:], spec))
