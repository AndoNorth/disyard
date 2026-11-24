"""`python -m dsy_placement.show` prints one line per task pool of the service's view, then the
snapshot's age and the error of its last read."""

import subprocess
import sys

from dsy_placement import show


def test_show_prints_each_pool(ci_api, running, until, capsys):
    ci_api.hold.clear()
    served = running()
    assert show.main(["--url", served.url]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "compile queue=dsy-compile slot=1000m/1024Mi ready_workers=none free=none images=none",
        "simulate queue=dsy-simulate slot=250m/512Mi ready_workers=none free=none images=none",
        "aggregate queue=dsy-aggregate slot=1000m/1024Mi ready_workers=none free=none images=none",
        "snapshot_age_s=none",
        "last_error=none",
    ]

    ci_api.hold.set()
    until(lambda: served.snapshotter.current()[0] is not None)
    result = subprocess.run(
        [sys.executable, "-m", "dsy_placement.show", "--url", served.url],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "compile queue=dsy-compile slot=1000m/1024Mi ready_workers=1 free=300m/110Mi"
        " images=dsy-compile-worker:tilt-6c1f2b9e0a4d7e31",
        "simulate queue=dsy-simulate slot=250m/512Mi ready_workers=1 free=50m/22Mi"
        " images=dsy-simulate-worker:tilt-0d5a8e3c7b2f1946",
        "aggregate queue=dsy-aggregate slot=1000m/1024Mi ready_workers=1 free=300m/110Mi"
        " images=dsy-aggregate-worker:tilt-9e4b1c7f2a6d0853",
        "snapshot_age_s=0",
        "last_error=none",
    ]


def test_show_prints_the_last_error(api, running, until, capsys):
    api.fail = 1_000
    served = running()
    until(lambda: served.snapshotter.current()[2] is not None)
    show.main(["--url", served.url])
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "snapshot_age_s=none",
        "last_error=HTTPError: HTTP Error 500: Internal Server Error",
    ]
