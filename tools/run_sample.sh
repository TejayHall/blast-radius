#!/bin/sh
# Build the image and run it like the scorer does on a data dir, then score it.
# CPU is read from the container's cgroup (cpu.stat usage_usec) after the selector exits.
# usage: tools/run_sample.sh <dir with commits/ and tests.json> [answers.json]
set -e
IMAGE=${IMAGE:-blast-radius-entry}
DATA_DIR=$1
ANSWERS=${2:-$DATA_DIR/answers.json}
docker build --platform linux/amd64 -q -t $IMAGE . >/dev/null
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$DATA_DIR/commits":/data/commits:ro \
  --volume "$DATA_DIR/tests.json":/data/tests.json:ro \
  $IMAGE sh -c 'python -m selector.main; echo "CPUSTAT $(grep usage_usec /sys/fs/cgroup/cpu.stat)" >&2' > output.jsonl 2> stderr.log
CPU_US=$(grep CPUSTAT stderr.log | awk '{print $3}')
echo "container cpu seconds: $(python -c "print($CPU_US/1e6)")"
py -3.13 ${SCORE_PY:-../kit/sample/score.py} "$DATA_DIR" "$ANSWERS" output.jsonl --cpu-seconds $(python -c "print($CPU_US/1e6)")
