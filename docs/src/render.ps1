# Renders docs/diagrams/svg/*.svg to docs/diagrams/*.png (2x) with headless Microsoft Edge.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$svgDir = Join-Path $root 'diagrams\svg'
$pngDir = Join-Path $root 'diagrams'
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$profile = Join-Path $env:TEMP 'serviceiq-edge-render'

Get-ChildItem $svgDir -Filter *.svg | ForEach-Object {
  $head = (Get-Content $_.FullName -TotalCount 1) -join ''
  $w = [regex]::Match($head, 'width="(\d+)"').Groups[1].Value
  $h = [regex]::Match($head, 'height="(\d+)"').Groups[1].Value
  $out = Join-Path $pngDir ($_.BaseName + '.png')
  $url = 'file:///' + ($_.FullName -replace '\\', '/')
  & $edge --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 "--user-data-dir=$profile" "--window-size=$w,$h" "--screenshot=$out" $url 2>$null | Out-Null
  Write-Output "$($_.BaseName): ${w}x${h}"
}
