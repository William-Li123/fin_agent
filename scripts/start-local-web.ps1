[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [string]$State = (Join-Path (Split-Path -Parent $PSScriptRoot) ".finagent"),
    [string]$PythonExe = "python",
    [string]$Frontend = (Join-Path (Split-Path -Parent $PSScriptRoot) "frontend/dist")
)

$ErrorActionPreference = "Stop"
$originalEnvironment = @{}
foreach ($variableName in @("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")) {
    $originalEnvironment[$variableName] = [Environment]::GetEnvironmentVariable($variableName, "Process")
}

try {
    $statePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($State)
    $frontendPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Frontend)
    if (-not (Test-Path -LiteralPath (Join-Path $frontendPath "index.html") -PathType Leaf)) {
        throw "前端尚未构建：请先在 frontend 目录执行 npm ci 和 npm run build，或通过 -Frontend 指定构建目录。"
    }

    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
        $localAppDataPath = [Environment]::GetFolderPath("LocalApplicationData")
        $cachePath = Join-Path $localAppDataPath "FinAgent/deepseek-key.dpapi"
        if (-not (Test-Path -LiteralPath $cachePath -PathType Leaf)) {
            throw "尚未配置 DeepSeek API Key：请设置当前进程的 DEEPSEEK_API_KEY，或使用当前 Windows 用户的本地加密凭据缓存。"
        }
        $secureKey = $null
        $keyPointer = [IntPtr]::Zero
        try {
            $encryptedKey = (Get-Content -LiteralPath $cachePath -Raw).Trim()
            $secureKey = ConvertTo-SecureString -String $encryptedKey
            $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
            $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
            if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
                throw "Empty cached credential"
            }
        }
        catch {
            throw "无法读取 DeepSeek 本地加密凭据：请使用创建缓存的 Windows 用户启动，或设置 DEEPSEEK_API_KEY。"
        }
        finally {
            if ($keyPointer -ne [IntPtr]::Zero) {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
            }
            if ($null -ne $secureKey) {
                $secureKey.Dispose()
            }
            $encryptedKey = $null
        }
    }

    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_BASE_URL)) {
        $env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    }
    if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_MODEL)) {
        $env:DEEPSEEK_MODEL = "deepseek-flash"
    }

    Write-Host "启动金融研究工作台：http://127.0.0.1:$Port；继续使用现有状态与预算账本。"
    & $PythonExe -X utf8 -m finagent --state $statePath web --port $Port --frontend $frontendPath
    if ($LASTEXITCODE -ne 0) {
        throw "金融研究服务退出，退出码为 $LASTEXITCODE。请检查 Python 安装和 Web 依赖。"
    }
}
finally {
    foreach ($variableName in $originalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($variableName, $originalEnvironment[$variableName], "Process")
    }
}
