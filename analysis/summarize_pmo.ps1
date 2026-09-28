param(
    [string]$DatasetRoot = (Join-Path $PSScriptRoot '..\..\..\pmo-dataset')
)

$pmeRoot = Join-Path (Resolve-Path $DatasetRoot) 'pme'
$rows = Get-ChildItem -LiteralPath $pmeRoot -Filter '*.json' |
    Sort-Object Name |
    ForEach-Object {
        $model = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
        $tasks = @($model.tasks).Count
        $gateways = @($model.gateways).Count
        $score = $tasks + 2 * $gateways
        $level = if ($score -le 22) { 'low' } elseif ($score -le 32) { 'medium' } else { 'high' }
        [pscustomobject]@{
            process = $_.BaseName
            tasks = $tasks
            gateways = $gateways
            events = @($model.events).Count
            sequence_flows = @($model.sequenceFlows).Count
            complexity_score = $score
            complexity_level = $level
        }
    }

$csvPath = Join-Path $PSScriptRoot 'pmo-complexity.csv'
$reportPath = Join-Path $PSScriptRoot 'pmo-complexity.md'
$rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding utf8

$taskStats = $rows | Measure-Object tasks -Minimum -Maximum -Average
$gatewayStats = $rows | Measure-Object gateways -Minimum -Maximum -Average
$levelCounts = @{
    low = @($rows | Where-Object complexity_level -eq 'low').Count
    medium = @($rows | Where-Object complexity_level -eq 'medium').Count
    high = @($rows | Where-Object complexity_level -eq 'high').Count
}

$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add('# PMO 流程复杂度统计')
$lines.Add('')
$lines.Add('数据源：PMo Dataset 1.0.0 的 `pme/*.json`（README 指定 BPMN/PME 为标准化 ground truth）。')
$lines.Add('')
$lines.Add('初始复杂度分数：`tasks + 2 × gateways`。按本数据集近似三等分：低 `≤22`、中 `23–32`、高 `≥33`。网关权重为 2，用于反映分支与汇合带来的额外控制流复杂度。')
$lines.Add('')
$lines.Add("- 流程数：$($rows.Count)")
$lines.Add("- Task：平均 $([math]::Round($taskStats.Average, 2))，范围 $([int]$taskStats.Minimum)–$([int]$taskStats.Maximum)")
$lines.Add("- Gateway：平均 $([math]::Round($gatewayStats.Average, 2))，范围 $([int]$gatewayStats.Minimum)–$([int]$gatewayStats.Maximum)")
$lines.Add("- 分档：低 $($levelCounts.low)，中 $($levelCounts.medium)，高 $($levelCounts.high)")
$lines.Add('')
$lines.Add('| 流程 | Tasks | Gateways | Events | Sequence flows | 分数 | 分档 |')
$lines.Add('|---:|---:|---:|---:|---:|---:|:---:|')
foreach ($row in $rows) {
    $lines.Add("| $($row.process) | $($row.tasks) | $($row.gateways) | $($row.events) | $($row.sequence_flows) | $($row.complexity_score) | $($row.complexity_level) |")
}
$lines | Set-Content -LiteralPath $reportPath -Encoding utf8

[pscustomobject]@{
    processes = $rows.Count
    csv = $csvPath
    report = $reportPath
    low = $levelCounts.low
    medium = $levelCounts.medium
    high = $levelCounts.high
}
