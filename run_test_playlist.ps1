param(
    [Parameter(Mandatory = $true)]
    [string]$SpotifyPlaylist,

    [string]$JellyfinPlaylist = "Spotify Test Import",

    [switch]$DryRun,

    [switch]$NoDownload,

    [switch]$InstallDeps,

    [ValidateSet("DEBUG", "INFO", "WARNING", "ERROR")]
    [string]$LogLevel = "INFO"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

$dockerBin = "C:\Program Files\Docker\Docker\resources\bin"
if ((Test-Path $dockerBin) -and -not (($env:Path -split ';') -contains $dockerBin)) {
    $env:Path = "$dockerBin;$env:Path"
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    throw "Python was not found on PATH. Install Python or launch from a shell where 'python' works."
}

if ($InstallDeps) {
    Write-Host "Installing Python dependencies..."
    & python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
}

if (-not $DryRun) {
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        throw "Docker CLI was not found. Start Docker Desktop and ensure C:\Program Files\Docker\Docker\resources\bin is on PATH."
    }

    Write-Host "Starting slskd container..."
    & docker compose up -d slskd
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start slskd with Docker Compose."
    }

    Write-Host "Waiting for slskd web API..."
    $slskdReady = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri "http://localhost:5030/" -Method Get -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                $slskdReady = $true
                break
            }
        }
        catch {
        }
        Start-Sleep -Seconds 2
    }

    if (-not $slskdReady) {
        throw "slskd did not become reachable on http://localhost:5030 within 60 seconds."
    }
}

$args = @(
    "main.py",
    "--spotify-playlist", $SpotifyPlaylist,
    "--log-level", $LogLevel
)

if ($DryRun) {
    $args += "--dry-run"
}
else {
    $args += @("--jellyfin-playlist", $JellyfinPlaylist)
}

if ($NoDownload) {
    $args += "--no-download"
}

Write-Host "Running sync..."
& python @args
if ($LASTEXITCODE -ne 0) {
    throw "main.py exited with code $LASTEXITCODE."
}