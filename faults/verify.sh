#!/usr/bin/env bash
# Verify that every fault and scripted candidate behaves as labelled.
# Maven runs inside Docker, offline (--network none, mvn -o), using the autoimmune-m2 volume.
# This is a labelling tool: it uses hidden_tests/, which the pipeline must never use.
#
# Usage: faults/verify.sh [F06 F01 F04 ...]
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MAVEN_IMAGE=maven:3.9-eclipse-temurin-17
SEMGREP_IMAGE=semgrep/semgrep:1.100.0
M2_VOLUME=autoimmune-m2
TEST_DIR=app/src/test/java/com/autoimmune/orders
FAULTS=${*:-F06 F01 F04}
EXISTING=$(cd "$ROOT/$TEST_DIR" && ls *.java | sed 's/\.java$//')

hostpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }
# Git Bash: stop docker's /src style container paths being rewritten (only for docker; git needs the conversion).
dockerx() { MSYS_NO_PATHCONV=1 docker "$@"; }

WORK=$(mktemp -d)
trap 'rm -rf "${WORK:?}"' EXIT
PASSED=0
FAILED=0

if ! docker volume inspect "$M2_VOLUME" >/dev/null 2>&1; then
  echo "Docker volume $M2_VOLUME not found. Run mvn test once with network to populate it." >&2
  exit 2
fi

# prepare DIR FAULT [CANDIDATE]: healthy app + inject.diff (+ candidate) + target and hidden tests.
# FAULT "healthy" means no diff, with the tests of every fault.
prepare() {
  local dir=$1 fault=$2 cand=${3:-} f
  mkdir -p "$dir" && cp -r "$ROOT/app" "$dir/" && rm -rf "${dir:?}/app/target" || return 1
  if [ "$fault" = healthy ]; then
    for f in $FAULTS; do cp "$ROOT/faults/$f"/target_test/*.java "$ROOT/faults/$f"/hidden_tests/*.java "$dir/$TEST_DIR/"; done
    return 0
  fi
  (cd "$dir" && git apply "$ROOT/faults/$fault/inject.diff") || { echo "  git apply failed: $fault/inject.diff"; return 1; }
  if [ -n "$cand" ]; then
    (cd "$dir" && git apply "$ROOT/faults/$fault/candidates/$cand.diff") || { echo "  git apply failed: $fault/candidates/$cand.diff"; return 1; }
  fi
  cp "$ROOT/faults/$fault"/target_test/*.java "$ROOT/faults/$fault"/hidden_tests/*.java "$dir/$TEST_DIR/"
}

run_tests() {
  dockerx run --rm --network none -v "$(hostpath "$1/app"):/src:ro" -v "$M2_VOLUME:/root/.m2" "$MAVEN_IMAGE" \
    sh -c 'cp -r /src /work && cd /work && mvn -B -o test -Dmaven.test.failure.ignore=true 2>&1' > "$1/mvn.log" 2>&1
  if ! grep -q "BUILD SUCCESS" "$1/mvn.log"; then
    echo "  maven did not complete (compile error or tool error); see tail of log:"
    grep -E "ERROR|BUILD" "$1/mvn.log" | head -10 | sed 's/^/    /'
  fi
  local failing
  failing=$(grep -E '<<< (FAILURE|ERROR)!' "$1/mvn.log" | grep -v 'Tests run:' \
    | sed -E 's/.*com\.autoimmune\.orders\.([A-Za-z0-9_]+\.[A-Za-z0-9_]+).*/\1/' | sort -u | tr '\n' ' ')
  echo "  failing tests: ${failing:-none}"
}

# class_status DIR CLASS -> pass | fail | notrun
class_status() {
  local line f e
  line=$(grep -E "Tests run: [0-9]+, Failures: [0-9]+, Errors: [0-9]+, Skipped: [0-9]+.* -- in com\.autoimmune\.orders\.$2$" "$1/mvn.log" | tail -1)
  [ -z "$line" ] && { echo notrun; return; }
  f=$(echo "$line" | sed -E 's/.*Failures: ([0-9]+).*/\1/')
  e=$(echo "$line" | sed -E 's/.*Errors: ([0-9]+).*/\1/')
  if [ "$f" -eq 0 ] && [ "$e" -eq 0 ]; then echo pass; else echo fail; fi
}

# group_status DIR CLASS... -> pass (all ran and passed) | fail (all ran, at least one failed) | notrun
group_status() {
  local dir=$1 c s any_fail=0
  shift
  for c in "$@"; do
    s=$(class_status "$dir" "$c")
    [ "$s" = notrun ] && { echo notrun; return; }
    [ "$s" = fail ] && any_fail=1
  done
  if [ $any_fail -eq 1 ]; then echo fail; else echo pass; fi
}

# semgrep_errors DIR -> number of ERROR-severity findings in production code, or "error"
semgrep_errors() {
  local out
  out=$(dockerx run --rm --network none -v "$(hostpath "$1/app"):/src:ro" -v "$(hostpath "$ROOT/config"):/rules:ro" \
    "$SEMGREP_IMAGE" semgrep scan --config /rules/semgrep.yml --severity ERROR --metrics=off \
    --disable-version-check --quiet --json /src/src/main/java 2>/dev/null) || { echo error; return; }
  echo "$out" | grep -o '"check_id"' | wc -l | tr -d ' '
}

check() {  # check DESCRIPTION ACTUAL EXPECTED
  if [ "$2" = "$3" ]; then
    echo "  PASS  $1"; PASSED=$((PASSED + 1))
  else
    echo "  FAIL  $1 (got: $2, expected: $3)"; FAILED=$((FAILED + 1))
  fi
}

echo "== healthy app with all target and hidden tests =="
d=$WORK/healthy
if prepare "$d" healthy; then
  run_tests "$d"
  all=$EXISTING
  for f in $FAULTS; do all="$all ${f}TargetTest ${f}HiddenTest"; done
  check "existing, target and hidden tests all pass on healthy code" "$(group_status "$d" $all)" pass
else
  check "healthy workspace prepared" no yes
fi

for f in $FAULTS; do
  T=${f}TargetTest
  H=${f}HiddenTest
  echo
  echo "== $f: $(grep -o '"description": "[^"]*"' "$ROOT/faults/$f/fault.json" | cut -d'"' -f4) =="

  echo "[inject]"
  d=$WORK/$f-inject
  base=error
  if prepare "$d" "$f"; then
    run_tests "$d"
    check "target test fails after inject.diff" "$(class_status "$d" "$T")" fail
    base=$(semgrep_errors "$d")
    echo "  semgrep ERROR findings (baseline): $base"
  else
    check "inject.diff applies" no yes
  fi

  for c in $(cd "$ROOT/faults/$f/candidates" && ls *.diff | sed 's/\.diff$//'); do
    echo "[$c]"
    d=$WORK/$f-$c
    if ! prepare "$d" "$f" "$c"; then check "$c.diff applies on top of inject.diff" no yes; continue; fi
    run_tests "$d"
    case $c in
      correct)
        check "target test passes" "$(class_status "$d" "$T")" pass
        check "hidden tests pass" "$(class_status "$d" "$H")" pass
        check "existing tests pass" "$(group_status "$d" $EXISTING)" pass
        n=$(semgrep_errors "$d"); echo "  semgrep ERROR findings: $n"
        check "no new semgrep ERROR findings" "$n" "$base"
        ;;
      overfitting)
        check "target test passes" "$(class_status "$d" "$T")" pass
        check "a hidden test fails" "$(class_status "$d" "$H")" fail
        ;;
      regressive)
        check "target test passes (symptom fixed)" "$(class_status "$d" "$T")" pass
        check "an existing test fails" "$(group_status "$d" $EXISTING)" fail
        ;;
      insecure)
        check "all tests pass" "$(group_status "$d" "$T" "$H" $EXISTING)" pass
        n=$(semgrep_errors "$d"); echo "  semgrep ERROR findings: $n"
        if [ "$n" != error ] && [ "$base" != error ] && [ "$n" -gt "$base" ]; then new=yes; else new=no; fi
        check "new semgrep ERROR finding" "$new" yes
        ;;
      slow)
        check "all tests pass" "$(group_status "$d" "$T" "$H" $EXISTING)" pass
        grep -q 'Thread.sleep(3000)' "$ROOT/faults/$f/candidates/$c.diff" && s=yes || s=no
        check "diff adds Thread.sleep(3000)" "$s" yes
        ;;
      *)
        echo "  (no checks defined for $c)"
        ;;
    esac
  done
done

echo
echo "verify: $PASSED passed, $FAILED failed"
[ $FAILED -eq 0 ]
