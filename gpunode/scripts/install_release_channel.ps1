[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ControlPlaneUrl,
    [Parameter(Mandatory=$true)][string]$EntryPointPath,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$ResidentTaskName,
    [Alias('PythonwPath')][string]$PythonPath="python.exe"
)

$ErrorActionPreference='Stop'
function Invoke-Schtasks {
    param([string[]]$Args)
    & schtasks.exe @Args
    if ($LASTEXITCODE -ne 0) { throw "schtasks failed: $LASTEXITCODE" }
}

if (-not (Test-Path -LiteralPath $EntryPointPath -PathType Leaf)) { throw 'Required existing node missing.' }
if (([uri]$ControlPlaneUrl).Scheme -ne 'https') { throw 'Control plane URL must use HTTPS.' }
$interpreterJson=& $PythonPath -c 'import json,sys; print(json.dumps({"executable":sys.executable,"base_executable":getattr(sys,"_base_executable",sys.executable)}))'
if ($LASTEXITCODE -ne 0) { throw 'Python interpreter probe failed.' }
try { $interpreters=$interpreterJson | ConvertFrom-Json } catch { throw 'Python interpreter probe returned invalid JSON.' }
if (-not $interpreters.executable -or -not $interpreters.base_executable) { throw 'Python interpreter probe returned incomplete paths.' }

$parent=Split-Path -Parent $InstallRoot
$stage=Join-Path $parent ('.release-stage-'+[guid]::NewGuid())
$backup=Join-Path $parent ('.release-backup-'+[guid]::NewGuid())
$taskXml=Join-Path $parent ('.resident-task-'+[guid]::NewGuid()+'.xml')
$moved=$false; $activatedNew=$false; $committed=$false

try {
    New-Item -ItemType Directory -Path $stage | Out-Null
    Invoke-Schtasks @('/Query','/TN',$ResidentTaskName,'/XML') | Set-Content -LiteralPath $taskXml -Encoding utf8
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\release_channel.py') -Destination (Join-Path $stage 'release_channel.py')
    Copy-Item -Path (Join-Path $PSScriptRoot '..\bootstrap\*') -Destination $stage -Recurse
    $configPath=Join-Path $stage 'release-channel.json'
    $config=@{control_plane_url=$ControlPlaneUrl;entrypoint_path=$EntryPointPath;entrypoint_python_path=$interpreters.executable;base_python_path=$interpreters.base_executable;poll_seconds=900} | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($configPath,$config,(New-Object System.Text.UTF8Encoding($false)))
    Invoke-Schtasks @('/End','/TN',$ResidentTaskName)
    if(Test-Path -LiteralPath $InstallRoot){ Move-Item -LiteralPath $InstallRoot -Destination $backup; $moved=$true }
    Move-Item -LiteralPath $stage -Destination $InstallRoot; $activatedNew=$true
    $command='"{0}" "{1}" --config "{2}" --watch' -f $interpreters.base_executable,(Join-Path $InstallRoot 'release_channel.py'),(Join-Path $InstallRoot 'release-channel.json')
    Invoke-Schtasks @('/Create','/TN',$ResidentTaskName,'/SC','ONSTART','/RL','LIMITED','/TR',$command,'/F')
    Invoke-Schtasks @('/Run','/TN',$ResidentTaskName)
    $committed=$true
}
catch {
    if(-not $committed){
        if($activatedNew -and (Test-Path -LiteralPath $InstallRoot)){ Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
        if($moved){ Move-Item -LiteralPath $backup -Destination $InstallRoot }
        if(Test-Path -LiteralPath $taskXml){
            Invoke-Schtasks @('/Create','/TN',$ResidentTaskName,'/XML',$taskXml,'/F')
            Invoke-Schtasks @('/Run','/TN',$ResidentTaskName)
        }
    }
    throw
}
finally {
    if(Test-Path -LiteralPath $stage){ Remove-Item -LiteralPath $stage -Recurse -Force }
}

try {
    if($committed -and $moved -and (Test-Path -LiteralPath $backup)){ Remove-Item -LiteralPath $backup -Recurse -Force }
    if($committed -and (Test-Path -LiteralPath $taskXml)){ Remove-Item -LiteralPath $taskXml -Force }
}
catch { }
if($committed){ exit 0 }
