smoke:
    scripts/smoke.sh

e2e *args:
    scripts/e2e.sh {{args}}

load-test:
    scripts/load-test.sh

characterize:
    scripts/characterize.sh
