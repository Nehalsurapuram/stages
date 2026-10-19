# Verify that every fault and scripted candidate behaves as labelled.
# Maven runs inside Docker, offline (--network none, mvn -o), using the autoimmune-m2 volume.
# This is a labelling tool: it uses hidden_tests/, which the pipeline must never use.
#
# Usage: powershell -ExecutionPolicy Bypass -File faults\verify.ps1 [F06 F01 F04 ...]
param([string[]]$Faults = @('F06', 'F01', 'F04'))

$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$MavenImage = 'maven:3.9-eclipse-temurin-17'
$SemgrepImage = 'semgrep/semgrep:1.100.0'
$M2Volume = 'autoimmune-m2'
$TestDir = 'app\src\test\java\com\autoimmune\orders'
$Existing = @(Get-ChildItem (Join-Path $Root $TestDir) -Filter *.java | ForEach-Object { $_.BaseName })
$MaxDiff = [int](Select-String -Path (Join-Path $Root 'config\decision.yaml') -Pattern '^max_diff_lines:\s*(\d+)').Matches[0].Groups[1].Value
$Work = Join-Path ([IO.Path]::GetTempPath()) ('autoimmune-verify-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$script:Passed = 0
$script:Failed = 0

& docker volume inspect $M2Volume *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker volume $M2Volume not found. Run mvn test once with network to populate it."
    exit 2
}

# Healthy app + inject.diff (+ candidate) + target and hidden tests. Fault 'healthy' = no diff, tests of every fault.
function Prepare([string]$Dir, [string]$Fault, [string]$Cand = '') {
    New-Item -ItemType Directory -Force $Dir | Out-Null
    Copy-Item (Join-Path $Root 'app') (Join-Path $Dir 'app') -Recurse
    $target = Join-Path $Dir 'app\target'
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    $tests = Join-Path $Dir $TestDir
    if ($Fault -eq 'healthy') {
        foreach ($f in $Faults) {
            Copy-Item (Join-Path $Root "faults\$f\target_test\*.java"), (Join-Path $Root "faults\$f\hidden_tests\*.java") $tests
        }
        return $true
    }
    $diffs = @(Join-Path $Root "faults\$Fault\inject.diff")
    if ($Cand) { $diffs += Join-Path $Root "faults\$Fault\candidates\$Cand.diff" }
    foreach ($diff in $diffs) {
        & git -C $Dir apply $diff
        if ($LASTEXITCODE -ne 0) { Write-Host "  git apply failed: $diff"; return $false }
    }
    Copy-Item (Join-Path $Root "faults\$Fault\target_test\*.java"), (Join-Path $Root "faults\$Fault\hidden_tests\*.java") $tests
    return $true
}

# Runs mvn test; returns hashtable class -> 'pass' | 'fail'. Classes that did not run are absent.
function Run-Tests([string]$Dir) {
    $lines = @(& docker run --rm --network none -v "$(Join-Path $Dir 'app'):/src:ro" -v "${M2Volume}:/root/.m2" $MavenImage `
            sh -c 'cp -r /src /work && cd /work && mvn -B -o test -Dmaven.test.failure.ignore=true 2>&1')
    $lines | Set-Content -Encoding utf8 (Join-Path $Dir 'mvn.log')
    if (-not ($lines -match 'BUILD SUCCESS')) {
        Write-Host '  maven did not complete (compile error or tool error); see tail of log:'
        $lines | Where-Object { $_ -match 'ERROR|BUILD' } | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" }
    }
    $failing = $lines | Where-Object { $_ -match '<<< (FAILURE|ERROR)!' -and $_ -notmatch 'Tests run:' } |
        ForEach-Object { if ($_ -match 'com\.autoimmune\.orders\.(\w+\.\w+)') { $Matches[1] } } | Sort-Object -Unique
    if ($failing) { Write-Host "  failing tests: $($failing -join ' ')" } else { Write-Host '  failing tests: none' }
    $result = @{}
    foreach ($l in $lines) {
        if ($l -match 'Tests run: \d+, Failures: (\d+), Errors: (\d+), Skipped: \d+.* -- in com\.autoimmune\.orders\.(\w+)$') {
            $result[$Matches[3]] = if ([int]$Matches[1] -eq 0 -and [int]$Matches[2] -eq 0) { 'pass' } else { 'fail' }
        }
    }
    return $result
}

function Class-Status($Results, [string]$Class) {
    if ($Results.ContainsKey($Class)) { return $Results[$Class] } else { return 'notrun' }
}

# pass = all ran and passed; fail = all ran, at least one failed; notrun = at least one did not run
function Group-Status($Results, [string[]]$Classes) {
    $anyFail = $false
    foreach ($c in $Classes) {
        $s = Class-Status $Results $c
        if ($s -eq 'notrun') { return 'notrun' }
        if ($s -eq 'fail') { $anyFail = $true }
    }
    if ($anyFail) { return 'fail' } else { return 'pass' }
}

# Semgrep findings in production code: @{ Error = n; Total = n }, or $null on tool error.
function Semgrep-Findings([string]$Dir) {
    $out = & docker run --rm --network none -v "$(Join-Path $Dir 'app'):/src:ro" -v "$(Join-Path $Root 'config'):/rules:ro" `
        $SemgrepImage semgrep scan --config /rules/semgrep.yml --metrics=off `
        --disable-version-check --quiet --json /src/src/main/java 2> $null
    if ($LASTEXITCODE -ne 0) { return $null }
    try { $res = @(($out -join "`n" | ConvertFrom-Json).results) } catch { return $null }
    return @{ Error = @($res | Where-Object { $_.extra.severity -eq 'ERROR' }).Count; Total = $res.Count }
}

function Format-Findings($s) { if ($null -eq $s) { 'error' } else { "$($s.Error) ERROR, $($s.Total) total" } }

function New-Findings($s, $Base, [string]$Key) {
    if ($null -eq $s -or $null -eq $Base) { return 'error' }
    return $s[$Key] - $Base[$Key]
}

function Check([string]$Description, $Actual, $Expected) {
    if ("$Actual" -eq "$Expected") {
        Write-Host "  PASS  $Description"; $script:Passed++
    } else {
        Write-Host "  FAIL  $Description (got: $Actual, expected: $Expected)"; $script:Failed++
    }
}

# AUTO_APPLY scope rules: the diff touches only the diagnosed file and stays within max_diff_lines.
function Check-Scope([string]$DiffPath, [string]$Diagnosed) {
    $lines = Get-Content $DiffPath
    $files = @($lines | ForEach-Object { if ($_ -match '^diff --git a/(\S+) b/') { $Matches[1] } })
    $changed = @($lines | Where-Object { $_ -match '^[+-]' -and $_ -notmatch '^(\+\+\+|---) (a/|b/|/dev/null)' }).Count
    $inside = ($files.Count -gt 0) -and (@($files | Where-Object { $_ -ne $Diagnosed }).Count -eq 0)
    Check "diff touches only the diagnosed file ($($files -join ', '))" $inside $true
    Check "diff within max_diff_lines ($changed <= $MaxDiff)" ($changed -le $MaxDiff) $true
}

try {
    Write-Host '== healthy app with all target and hidden tests =='
    $d = Join-Path $Work 'healthy'
    if (Prepare $d 'healthy') {
        $r = Run-Tests $d
        $all = $Existing + ($Faults | ForEach-Object { "${_}TargetTest"; "${_}HiddenTest" })
        Check 'existing, target and hidden tests all pass on healthy code' (Group-Status $r $all) 'pass'
    } else { Check 'healthy workspace prepared' 'no' 'yes' }

    foreach ($f in $Faults) {
        $T = "${f}TargetTest"
        $H = "${f}HiddenTest"
        $fault = Get-Content (Join-Path $Root "faults\$f\fault.json") -Raw | ConvertFrom-Json
        Write-Host ''
        Write-Host "== ${f}: $($fault.description) =="

        Write-Host '[inject]'
        $d = Join-Path $Work "$f-inject"
        $base = $null
        if (Prepare $d $f) {
            $r = Run-Tests $d
            Check 'target test fails after inject.diff' (Class-Status $r $T) 'fail'
            Check "existing suite (V2) still passes under inject.diff alone ($($Existing -join ', '))" (Group-Status $r $Existing) 'pass'
            $base = Semgrep-Findings $d
            Write-Host "  semgrep findings (baseline): $(Format-Findings $base)"
        } else { Check 'inject.diff applies' 'no' 'yes' }

        foreach ($cf in Get-ChildItem (Join-Path $Root "faults\$f\candidates") -Filter *.diff | Sort-Object Name) {
            $c = $cf.BaseName
            Write-Host "[$c]"
            $d = Join-Path $Work "$f-$c"
            if (-not (Prepare $d $f $c)) { Check "$c.diff applies on top of inject.diff" 'no' 'yes'; continue }
            $r = Run-Tests $d
            switch ($c) {
                'correct' {
                    Check 'target test passes' (Class-Status $r $T) 'pass'
                    Check 'hidden tests pass' (Class-Status $r $H) 'pass'
                    Check 'existing tests pass' (Group-Status $r $Existing) 'pass'
                    $n = Semgrep-Findings $d; Write-Host "  semgrep findings: $(Format-Findings $n)"
                    Check 'no new semgrep findings' (New-Findings $n $base 'Total') 0
                    Check-Scope $cf.FullName $fault.diagnosed_file
                }
                'overfitting' {
                    Check 'target test passes' (Class-Status $r $T) 'pass'
                    Check 'a hidden test fails' (Class-Status $r $H) 'fail'
                }
                'regressive' {
                    Check 'target test passes (symptom fixed)' (Class-Status $r $T) 'pass'
                    Check 'an existing test fails' (Group-Status $r $Existing) 'fail'
                }
                'insecure' {
                    Check 'all tests pass' (Group-Status $r (@($T, $H) + $Existing)) 'pass'
                    $n = Semgrep-Findings $d; Write-Host "  semgrep findings: $(Format-Findings $n)"
                    $new = New-Findings $n $base 'Error'
                    Check 'new semgrep ERROR finding' ("$new" -ne 'error' -and [int]$new -gt 0) $true
                }
                'slow' {
                    Check 'all tests pass (target, hidden, existing)' (Group-Status $r (@($T, $H) + $Existing)) 'pass'
                    $s = if (Select-String -Path $cf.FullName -SimpleMatch 'Thread.sleep(3000)' -Quiet) { 'yes' } else { 'no' }
                    Check 'diff adds Thread.sleep(3000)' $s 'yes'
                    $n = Semgrep-Findings $d; Write-Host "  semgrep findings: $(Format-Findings $n)"
                    Check 'no new semgrep findings' (New-Findings $n $base 'Total') 0
                    Check-Scope $cf.FullName $fault.diagnosed_file
                }
                'ineffective' {
                    Check 'target test still fails (change does not fix the bug)' (Class-Status $r $T) 'fail'
                }
                default { Write-Host "  (no checks defined for $c)" }
            }
        }
    }
} finally {
    if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
}

Write-Host ''
Write-Host "verify: $($script:Passed) passed, $($script:Failed) failed"
if ($script:Failed -eq 0) { exit 0 } else { exit 1 }
