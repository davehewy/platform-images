[CmdletBinding()]
param(
    [string] $Version,
    [string] $InstallDir
)

$ErrorActionPreference = "Stop"
$repository = "davehewy/platform-images"

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = if ($env:PLATFORM_IMAGES_VERSION) { $env:PLATFORM_IMAGES_VERSION } else { "latest" }
}
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = if ($env:PLATFORM_IMAGES_INSTALL_DIR) {
        $env:PLATFORM_IMAGES_INSTALL_DIR
    } else {
        Join-Path $env:LOCALAPPDATA "Programs\platform-images"
    }
}

$architecture = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64" { "amd64" }
    "Arm64" { "arm64" }
    default {
        throw "platform-images: unsupported Windows architecture: $($_)"
    }
}

if ($env:PLATFORM_IMAGES_RELEASE_URL) {
    if ($Version -eq "latest") {
        throw "platform-images: PLATFORM_IMAGES_VERSION is required with PLATFORM_IMAGES_RELEASE_URL"
    }
    $releaseVersion = if ($Version.StartsWith("v")) { $Version.Substring(1) } else { $Version }
    $releaseUrl = $env:PLATFORM_IMAGES_RELEASE_URL.TrimEnd("/")
} elseif ($Version -eq "latest") {
    $latestRelease = Invoke-RestMethod -Uri "https://api.github.com/repos/$repository/releases/latest"
    $tag = [string] $latestRelease.tag_name
    if (-not $tag.StartsWith("v")) {
        throw "platform-images: unable to resolve the latest release version"
    }
    $releaseVersion = $tag.Substring(1)
    $releaseUrl = "https://github.com/$repository/releases/download/$tag"
} else {
    $releaseVersion = if ($Version.StartsWith("v")) { $Version.Substring(1) } else { $Version }
    $tag = "v$releaseVersion"
    $releaseUrl = "https://github.com/$repository/releases/download/$tag"
}
if ($releaseVersion -notmatch '^[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*$') {
    throw "platform-images: invalid release version: $releaseVersion"
}

$asset = "platform-images-v$releaseVersion-windows-$architecture.zip"
$legacyAsset = "platform-images-windows-$architecture.zip"

$temporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) (
    "platform-images-install-" + [System.Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null

try {
    $archive = Join-Path $temporaryDirectory $asset
    $checksums = Join-Path $temporaryDirectory "SHA256SUMS"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseUrl/$asset" -OutFile $archive
    } catch {
        $asset = $legacyAsset
        $archive = Join-Path $temporaryDirectory $asset
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseUrl/$asset" -OutFile $archive
    }
    Invoke-WebRequest -UseBasicParsing -Uri "$releaseUrl/SHA256SUMS" -OutFile $checksums

    $expected = $null
    foreach ($line in Get-Content $checksums) {
        if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)$' -and $Matches[2] -eq $asset) {
            $expected = $Matches[1]
            break
        }
    }
    if (-not $expected) {
        throw "platform-images: $asset is absent from SHA256SUMS"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash
    if ($actual -ne $expected) {
        throw "platform-images: checksum verification failed for $asset"
    }

    $expanded = Join-Path $temporaryDirectory "expanded"
    Expand-Archive -Path $archive -DestinationPath $expanded
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -Force (Join-Path $expanded "platform.exe") (Join-Path $InstallDir "platform.exe")

    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $pathEntries = @($userPath -split ";" | Where-Object { $_ })
    if ($pathEntries -notcontains $InstallDir) {
        $newPath = (@($pathEntries) + $InstallDir) -join ";"
        [System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "Added $InstallDir to your user PATH; open a new terminal to use it."
    }
    Write-Host "Installed platform-images v$releaseVersion to $(Join-Path $InstallDir 'platform.exe')"
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $temporaryDirectory
}
