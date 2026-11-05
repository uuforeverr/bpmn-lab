param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'bpmn-lab\bpmndiffviz')
)

$ErrorActionPreference = 'Stop'
$PinnedCommit = '01554efe25b39fd4a601e661bd559a29795cfd41'
$Repository = 'https://bitbucket.org/sivanov68/bpmndiffviz.git'
$CliSource = Join-Path $PSScriptRoot '..\tools\bpmndiffviz\BpmnDiffCli.java'
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

foreach ($command in 'git', 'mvn', 'java', 'javac') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command is unavailable: $command"
    }
}

if (Test-Path -LiteralPath $InstallDir) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir '.git'))) {
        throw "InstallDir exists but is not a Git checkout: $InstallDir"
    }
    $dirty = & git -C $InstallDir status --porcelain
    if ($LASTEXITCODE -ne 0 -or $dirty) {
        throw "Refusing to change a dirty BPMNDiffViz checkout: $InstallDir"
    }
} else {
    New-Item -ItemType Directory -Path (Split-Path $InstallDir -Parent) -Force | Out-Null
    & git clone $Repository $InstallDir
    if ($LASTEXITCODE -ne 0) { throw 'BPMNDiffViz clone failed.' }
}

& git -C $InstallDir fetch origin $PinnedCommit
if ($LASTEXITCODE -ne 0) { throw 'BPMNDiffViz fetch failed.' }
& git -C $InstallDir checkout --detach $PinnedCommit
if ($LASTEXITCODE -ne 0) { throw 'BPMNDiffViz checkout failed.' }

Push-Location $InstallDir
try {
    & mvn -q -DskipTests package
    if ($LASTEXITCODE -ne 0) { throw 'BPMNDiffViz Maven build failed.' }
    $ClassPath = "target\classes$([System.IO.Path]::PathSeparator)target\BPMNDiffViz\WEB-INF\lib\*"
    & javac -cp $ClassPath -d 'target\classes' $CliSource
    if ($LASTEXITCODE -ne 0) { throw 'BpmnDiffCli compilation failed.' }
} finally {
    Pop-Location
}

Write-Output $InstallDir
