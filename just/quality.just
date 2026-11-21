test:
    scripts/test.sh

itest *domain:
    scripts/itest.sh {{ domain }}

contracts-map:
    scripts/check-contracts-map.sh

lint:
    scripts/lint.sh

ci *mode:
    scripts/ci.sh {{ mode }}

pre-pr *base:
    scripts/pre-pr.sh {{ base }}

pre-push *check:
    scripts/hooks/pre-push.sh {{ check }}

hooks-status:
    scripts/hooks/status.sh

scan *target:
    scripts/scan.sh {{ target }}
