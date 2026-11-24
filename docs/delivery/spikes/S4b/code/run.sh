#!/usr/bin/env bash
# Spike S4b: builds python-qpid-proton from its sdist for each version under test, in a scratch venv
# of the dev shell's Python and in a build stage on the pinned python image, then runs the five S4
# checks three times per version and the measurements once, each against a fresh RabbitMQ container
# under one dsy_itest T1 lock. Prints one `figure <name> <value>` line per number. Run in the dev shell:
#   nix develop -c docs/delivery/spikes/S4b/code/run.sh [out-dir] [--only-checks|--only-measure]
# Exit 0 the run completed (the verdict is a figure), 1 a step failed, 2 usage.
set -uo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)
code=$root/docs/delivery/spikes/S4b/code
out=$(realpath -m "${1:-$(mktemp -d "${TMPDIR:-/tmp}/dsy-s4b.XXXXXX")}")
mode=${2:-all}
case $mode in all | --only-checks | --only-measure) ;; *) echo "usage: run.sh [out-dir] [--only-checks|--only-measure]" >&2; exit 2 ;; esac
mkdir -p "$out"
# The T1 port slot defaults to the pool slot of this checkout, so two worktrees never share ports.
export DSY_SLOT=${DSY_SLOT:-$(basename "$(dirname "$root")")}
[[ $DSY_SLOT =~ ^[0-9]$ ]] || export DSY_SLOT=0
say() { echo "[$(date +%T)] $*" >&2; }
figure() { echo "figure $1 $2" | tee -a "$out/figures.txt"; }
: >"$out/figures.txt"
docker info >/dev/null 2>&1 || { say "docker daemon unreachable"; exit 1; }
base="python:$(yq '.images.python.version' "$root/deploy/versions.yaml")@$(yq '.images.python.digest' "$root/deploy/versions.yaml")"
rc=0

figure host.mem_total_mib "$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo)"
figure host.mem_available_mib.before "$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)"
figure host.cpus "$(nproc)"
figure host.docker "$(docker version --format '{{.Server.Version}}')"
figure host.other_containers "$(docker ps -q | wc -l)"
figure devshell.cc "$(command -v cc || echo none)"
figure devshell.cc_from_nix "$(command -v cc | grep -q '^/nix/' && echo yes || echo no)"
figure image.python "$base"
figure image.rabbitmq "$(docker image inspect --format '{{index .RepoDigests 0}}' rabbitmq:4.4.0 2>/dev/null || echo not-pulled)"

for proton in 0.40.0 0.41.1; do
    venv=$out/venv-$proton
    say "build proton $proton from its sdist under uv"
    start=$(date +%s.%N)
    uv venv -q --clear --python "$(command -v python3)" "$venv" &&
        UV_CACHE_DIR=$out/uv-cache uv pip install -q -p "$venv/bin/python" --no-binary python-qpid-proton \
            "python-qpid-proton==$proton" "$root/packages/itest" 2>"$out/uv-$proton.log"
    built=$?
    figure "proton$proton.devshell.build" "$([ $built -eq 0 ] && echo ok || echo failed)"
    figure "proton$proton.devshell.build_s" "$(echo "$(date +%s.%N) - $start" | bc | xargs printf '%.1f')"
    say "build proton $proton in a build stage on $base"
    start=$(date +%s.%N)
    image=failed
    docker build -q --build-arg "BASE=$base" --build-arg "PROTON=$proton" -t "dsy-s4b-proton:$proton" \
        -f "$code/Dockerfile" "$code" >"$out/docker-$proton.log" 2>&1 && image=ok
    figure "proton$proton.image.build" "$image"
    figure "proton$proton.image.build_s" "$(echo "$(date +%s.%N) - $start" | bc | xargs printf '%.1f')"
    figure "proton$proton.image.import" "$(docker run --rm "dsy-s4b-proton:$proton" python -c \
        'import proton, sys; print(".".join(map(str, proton.VERSION)) + "/py" + sys.version.split()[0])' 2>&1)"
    docker image rm -f "dsy-s4b-proton:$proton" >/dev/null 2>&1
    [ $built -eq 0 ] || { rc=1; continue; }
    if [ "$mode" != --only-measure ]; then
        say "checks 1 to 5, three runs, proton $proton"
        (cd "$code" && "$venv/bin/python" s4b.py checks --runs 3 --out "$out/work") | tee -a "$out/figures.txt"
        [ "${PIPESTATUS[0]}" -eq 0 ] || rc=1
    fi
    if [ "$mode" != --only-checks ] && [ "$proton" = 0.40.0 ]; then
        say "measurements 6 to 11 and 13, proton $proton"
        (cd "$code" && "$venv/bin/python" s4b.py measure --out "$out/work") | tee -a "$out/figures.txt"
        [ "${PIPESTATUS[0]}" -eq 0 ] || rc=1
    fi
done

figure host.mem_available_mib.after "$(awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo)"
figure containers.left.label_all "$(docker ps -aq --filter label=dsy.dev/verify-run --filter name=s4b | wc -l)"
exit $rc
