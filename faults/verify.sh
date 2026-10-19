#!/usr/bin/env bash
# Verify that every fault and scripted candidate behaves as labelled.
# Maven runs inside Docker, offline (--network none, mvn -o), using the autoimmune-m2 volume.
# This is a labelling tool: it uses hidden_tests/, which the pipeline must never use.
#
# Usage: bash faults/verify.sh [F06 F01 F04 ...]
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MAVEN_IMAGE=maven:3.9-eclipse-temurin-17
SEMGREP_IMAGE=semgrep/semgrep:1.100.0
M2_VOLUME=autoimmune-m2
TEST_DIR=app/src/test/java/com/autoimmune/orders
FAULTS=${*:-F06 F01 F04}
EXISTING=$(cd "$ROOT/$TEST_DIR" && ls *.java | sed 's/\.java$//')
MAX_DIFF=$(sed -nE 's/^max_diff_lines:[[:space:]]*([0-9]+).*/\1/p' "$ROOT/config/decision.yaml")

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

# semgrep_scan DIR: writes DIR/semgrep.json; returns non-zero on tool error
semgrep_scan() {
  dockerx run --rm --network none -v "$(hostpath "$1/app"):/src:ro" -v "$(hostpath "$ROOT/config"):/rules:ro" \
    "$SEMGREP_IMAGE" semgrep scan --config /rules/semgrep.yml --metrics=off \
    --disable-version-check --quiet --json /src/src/main/java > "$1/semgrep.json" 2>/dev/null
}
# findings DIR total|error -> count, or "error"
findings() {
  [ -s "$1/semgrep.json" ] || { echo error; return; }
  if [ "$2" = error ]; then
    grep -oE '"severity": ?"ERROR"' "$1/semgrep.json" | wc -l | tr -d ' '
  else
    grep -o '"check_id"' "$1/semgrep.json" | wc -l | tr -d ' '
  fi
}
# new_findings DIR BASEDIR total|error -> difference, or "error"
new_findings() {
  local n b
  n=$(findings "$1" "$3"); b=$(findings "$2" "$3")
  if [ "$n" = error ] || [ "$b" = error ]; then echo error; else echo $((n - b)); fi
}
show_findings() { echo "  semgrep findings${2:-}: $(findings "$1" error) ERROR, $(findings "$1" total) total"; }

check() {  # check DESCRIPTION ACTUAL EXPECTED
  if [ "$2" = "$3" ]; then
    echo "  PASS  $1"; PASSED=$((PASSED + 1))
  else
    echo "  FAIL  $1 (got: $2, expected: $3)"; FAILED=$((FAILED + 1))
  fi
}

# check_scope DIFF DIAGNOSED: AUTO_APPLY scope rules (only the diagnosed file, within max_diff_lines)
check_scope() {
  local files changed inside=false
  files=$(grep -E '^diff --git a/' "$1" | sed -E 's#^diff --git a/([^ ]+) b/.*#\1#')
  changed=$(grep -E '^[+-]' "$1" | grep -vcE '^(\+\+\+|---) (a/|b/|/dev/null)')
  if [ -n "$files" ] && [ -z "$(echo "$files" | grep -vxF "$2")" ]; then inside=true; fi
  check "diff touches only the diagnosed file ($(echo $files))" "$inside" true
  [ "$changed" -le "$MAX_DIFF" ] && within=true || within=false
  check "diff within max_diff_lines ($changed <= $MAX_DIFF)" "$within" true
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
  FJ=$ROOT/faults/$f/fault.json
  DIAG=$(grep -o '"diagnosed_file": "[^"]*"' "$FJ" | cut -d'"' -f4)
  echo
  echo "== $f: $(grep -o '"description": "[^"]*"' "$FJ" | cut -d'"' -f4) =="

  echo "[inject]"
  base=$WORK/$f-inject
  if prepare "$base" "$f"; then
    run_tests "$base"
    check "target test fails after inject.diff" "$(class_status "$base" "$T")" fail
    check "existing suite (V2) still passes under inject.diff alone ($(echo $EXISTING | tr ' ' ','))" "$(group_status "$base" $EXISTING)" pass
    semgrep_scan "$base"
    show_findings "$base" " (baseline)"
  else
    check "inject.diff applies" no yes
  fi

  for c in $(cd "$ROOT/faults/$f/candidates" && ls *.diff | sed 's/\.diff$//'); do
    echo "[$c]"
    d=$WORK/$f-$c
    cf=$ROOT/faults/$f/candidates/$c.diff
    if ! prepare "$d" "$f" "$c"; then check "$c.diff applies on top of inject.diff" no yes; continue; fi
    run_tests "$d"
    case $c in
      correct)
        check "target test passes" "$(class_status "$d" "$T")" pass
        check "hidden tests pass" "$(class_status "$d" "$H")" pass
        check "existing tests pass" "$(group_status "$d" $EXISTING)" pass
        semgrep_scan "$d"; show_findings "$d"
        check "no new semgrep findings" "$(new_findings "$d" "$base" total)" 0
        check_scope "$cf" "$DIAG"
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
        semgrep_scan "$d"; show_findings "$d"
        n=$(new_findings "$d" "$base" error)
        if [ "$n" != error ] && [ "$n" -gt 0 ]; then new=true; else new=false; fi
        check "new semgrep ERROR finding" "$new" true
        ;;
      slow)
        check "all tests pass (target, hidden, existing)" "$(group_status "$d" "$T" "$H" $EXISTING)" pass
        grep -q 'Thread.sleep(3000)' "$cf" && s=yes || s=no
        check "diff adds Thread.sleep(3000)" "$s" yes
        semgrep_scan "$d"; show_findings "$d"
        check "no new semgrep findings" "$(new_findings "$d" "$base" total)" 0
        check_scope "$cf" "$DIAG"
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
