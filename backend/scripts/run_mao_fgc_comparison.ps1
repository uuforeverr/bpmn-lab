param(
    [int]$Repetitions = 1,
    [int]$Seed = 20261023,
    [string]$RunName = 'mao-fgc-gpt4',
    [string]$ConfigName = 'config.mao-fgc-gpt4.yaml',
    [switch]$Resume,
    [switch]$SkipBpmnDiff
)

$ErrorActionPreference = 'Stop'
if ($ConfigName -eq 'config.mao-fgc-gpt4.yaml' -and -not $env:OPENAI_API_KEY) {
    throw 'OPENAI_API_KEY is not set. Export an authorized OpenAI API key before running this experiment.'
}
if ($Repetitions -lt 1) { throw 'Repetitions must be at least 1.' }

$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Backend = Join-Path $RepoRoot 'backend'
$Dataset = Join-Path (Split-Path $RepoRoot -Parent) '数据集\pmo-dataset'
$Config = Join-Path $Backend $ConfigName
if (-not (Test-Path -LiteralPath $Config)) { throw "Config does not exist: $Config" }
$ResultDir = Join-Path $Backend "results\$RunName"
$Artifacts = Join-Path $ResultDir 'bpmn'
$Report = Join-Path $ResultDir 'generation.json'
$DiffReport = Join-Path $ResultDir 'bpmndiffviz.json'
$Python = Join-Path $Backend '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python' }

if ((Test-Path -LiteralPath $Report) -and -not $Resume) {
    throw "Result already exists: $Report. Use -Resume or choose another -RunName."
}
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:3001/health' -TimeoutSec 3
} catch {
    throw "Layout service is not running. Start it with: cd '$RepoRoot\layout-service'; npm start"
}
if (-not $health) { throw 'Layout service health check returned an empty response.' }

New-Item -ItemType Directory -Path $Artifacts -Force | Out-Null
$Arguments = @(
    '-m', 'scripts.compare_strategies',
    '--dataset', $Dataset,
    '--config', $Config,
    '--samples', '21', '22', '23', '24',
    '--include-unsupported',
    '--strategies', 'semantic',
    '--repetitions', $Repetitions,
    '--seed', $Seed,
    '--prompt-language', 'en',
    '--skip-quality-metrics',
    '--output', $Report,
    '--bpmn-output-dir', $Artifacts
)
if ($Resume) { $Arguments += '--resume' }

Push-Location $Backend
try {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Generation failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

if (-not $SkipBpmnDiff) {
    $BpmnDiffDir = & (Join-Path $PSScriptRoot 'setup_bpmndiffviz.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'BPMNDiffViz setup failed.' }
    & $Python (Join-Path $PSScriptRoot 'evaluate_mao_fgc_bpmndiff.py') `
        --generated-dir $Artifacts `
        --reference-dir (Join-Path $Dataset 'bpmn') `
        --bpmndiffviz-dir $BpmnDiffDir `
        --output $DiffReport
    if ($LASTEXITCODE -ne 0) { throw 'BPMNDiffViz evaluation failed.' }
}

Write-Output "generation=$Report"
if (-not $SkipBpmnDiff) { Write-Output "bpmndiffviz=$DiffReport" }
