#requires -Version 5.1
<#
.SYNOPSIS
    从零安装 Qwen-Image-2.1 本地出图环境（Windows + NVIDIA 显卡）。

.DESCRIPTION
    做四件事：检查硬件与前置条件、准备 uv 环境、下载推理程序与模型并逐一校验
    SHA256、解压落位。全部下载支持断点续传，失败可重复执行，已完成的步骤会自动跳过。

    推理程序与模型版本都钉死在实测通过的组合上，不会因为上游更新而变化。

.PARAMETER Proxy
    统一代理，例如 http://127.0.0.1:7890。不传则读取环境变量 HTTP_PROXY / HTTPS_PROXY。
    本脚本会把该代理同时用于 curl 下载和子进程环境变量。

.PARAMETER WithFallback
    额外下载普通 Q4_K_M 量化（4.6 GB）作为画质/速度备选。默认不下载。

.PARAMETER Force
    忽略已存在的文件，强制重新下载。

.EXAMPLE
    .\setup.ps1
.EXAMPLE
    .\setup.ps1 -Proxy http://127.0.0.1:7890
.EXAMPLE
    .\setup.ps1 -WithFallback
#>
param(
    [string]$Proxy = '',
    [switch]$WithFallback,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$BinDir = Join-Path $Root 'bin'
$Models = Join-Path $Root 'models'
$Downloads = Join-Path $Root 'downloads'
$Temp = Join-Path $Downloads 'extract'

# ---------------------------------------------------------------- 输出小工具

function Say([string]$Text) { Write-Host $Text }
function Step([string]$Text) { Write-Host ''; Write-Host ("== " + $Text + " " + ('=' * [Math]::Max(0, 60 - $Text.Length))) -ForegroundColor Cyan }
function Ok([string]$Text) { Write-Host ("   [完成] " + $Text) -ForegroundColor Green }
function Warn([string]$Text) { Write-Host ("   [注意] " + $Text) -ForegroundColor Yellow }
function Fail([string]$Text) { Write-Host ("   [失败] " + $Text) -ForegroundColor Red }

# ---------------------------------------------------------------- 代理

if (-not $Proxy) {
    foreach ($name in @('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy')) {
        if ([Environment]::GetEnvironmentVariable($name)) { $Proxy = [Environment]::GetEnvironmentVariable($name); break }
    }
}
if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    Say ("使用代理：" + $Proxy)
} else {
    Say "未设置代理，直连下载。国内网络通常需要代理才能访问 GitHub 与 HuggingFace。"
}

# ---------------------------------------------------------------- 下载清单

# 发布标签是 master-889-c678dfe，但资产文件名里用的是短哈希 c678dfe，两者不同
$SdCppTag   = 'master-889-c678dfe'
$SdCppShort = 'c678dfe'
$SdCppBase  = "https://github.com/leejet/stable-diffusion.cpp/releases/download/$SdCppTag"

$Items = @(
    @{ Name = 'stable-diffusion.cpp 推理程序（Windows CUDA12）'
       Url  = "$SdCppBase/sd-master-$SdCppShort-bin-win-cuda12-x64.zip"
       Dest = (Join-Path $Downloads "sd-master-$SdCppShort-bin-win-cuda12-x64.zip")
       Sha  = 'caa31c81523613fa02f6af4c7a52f733c8fd406cfa081a15656a0555f1abc55d'
       Size = 332970080
       Kind = 'zip' }

    @{ Name = 'CUDA 运行库（cudart / cuBLAS）'
       Url  = "$SdCppBase/cudart-sd-bin-win-cu12-x64.zip"
       Dest = (Join-Path $Downloads 'cudart-sd-bin-win-cu12-x64.zip')
       Sha  = 'fe20366827d357c00797eebb58244dddab7fd9a348d70090c3871004c320f38d'
       Size = 563452046
       Kind = 'zip' }

    @{ Name = '扩散模型 HQv3 混合精度（已修正 sd.cpp 兼容性）'
       Url  = 'https://huggingface.co/zcf0508/qwen-image-2.1-hqv3-sdcpp-fixed/resolve/main/Qwen-Image-2.1-Q4-sd.cpp.gguf'
       Dest = (Join-Path $Models 'Qwen-Image-2.1-Q4_K_M-HQv3.gguf')
       Sha  = '86539a0613f82204f785d484ed95407fa7c5a50ce618cefe1048483986bb0e25'
       Size = 5959127264
       Kind = 'file' }

    @{ Name = '文本编码器 Qwen3VL-8B-Instruct Q4_K_M'
       Url  = 'https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf'
       Dest = (Join-Path $Models 'text_encoders\Qwen3VL-8B-Instruct-Q4_K_M.gguf')
       Sha  = '67d1659bfe71b89d50b45a4ad1a9e5b997e5bb16ce5da66a6a6167abd569e9e2'
       Size = 5027784800
       Kind = 'file' }

    @{ Name = '视觉塔 mmproj（仅图像编辑需要）'
       Url  = 'https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-F16.gguf'
       Dest = (Join-Path $Models 'text_encoders\mmproj-Qwen3VL-8B-Instruct-F16.gguf')
       Sha  = 'ca524100ebf825c9a870db1c580d03879e0da0ab2541697e2458e64891cf9d38'
       Size = 1159029824
       Kind = 'file' }

    @{ Name = 'VAE'
       Url  = 'https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/resolve/main/qwen_image_2.1_vae_bf16.safetensors'
       Dest = (Join-Path $Models 'vae\qwen_image_2.1_vae_bf16.safetensors')
       Sha  = 'bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9'
       Size = 675509688
       Kind = 'file' }
)

if ($WithFallback) {
    $Items += @{ Name = '扩散模型 普通 Q4_K_M（备选）'
                 Url  = 'https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF/resolve/main/qwen-image-2.1-Q4_K_M.gguf'
                 Dest = (Join-Path $Models 'qwen-image-2.1-Q4_K_M.gguf')
                 Sha  = '833439e91bc1152d28f37aa198c7f6f4218b7de95754c2f7a318a2422ab4b2f8'
                 Size = 4604557984
                 Kind = 'file' }
}

# ---------------------------------------------------------------- 第一步：环境检查

Step '第一步 / 环境检查'

$problems = @()

if ($env:OS -ne 'Windows_NT') { $problems += '本脚本只支持 Windows。' }

$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi) {
    $problems += '找不到 nvidia-smi，请先安装 NVIDIA 显卡驱动。'
} else {
    $line = & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
    if ($line) {
        Say ("   显卡：" + $line)
        $vramMb = [int](($line -split ',')[1] -replace '[^0-9]', '')
        if ($vramMb -lt 11000) {
            $problems += ("显存只有 " + [math]::Round($vramMb / 1024, 1) + " GB，低于本配置实测所需的 12 GB。可以继续，但大尺寸出图会失败。")
        }
    } else {
        $problems += 'nvidia-smi 没有返回显卡信息，驱动可能异常。'
    }
}

$ramGb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
Say ("   内存：" + $ramGb + " GB")
if ($ramGb -lt 30) { $problems += '系统内存低于 32 GB，文本编码器常驻会吃紧。' }

$freeGb = [math]::Round((Get-PSDrive -Name ($Root.Substring(0, 1))).Free / 1GB, 1)
Say ("   " + $Root.Substring(0, 1) + " 盘可用空间：" + $freeGb + " GB")
if ($freeGb -lt 25) { $problems += ('磁盘可用空间不足 25 GB（只有 ' + $freeGb + ' GB）。') }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $problems += @'
没有找到 uv。请先安装（任选其一）：
       winget install --id astral-sh.uv
       powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
     安装后重开一个终端再运行本脚本。
'@
} else {
    Say ("   uv：" + ((& uv --version) -join ' '))
}

if ($problems.Count) {
    Say ''
    foreach ($p in $problems) { Fail $p }
    Say ''
    Fail '环境检查未通过，已停止。'
    exit 1
}
Ok '环境检查通过'

# ---------------------------------------------------------------- 第二步：目录

Step '第二步 / 建立目录'
foreach ($dir in @($BinDir, $Models, (Join-Path $Models 'text_encoders'), (Join-Path $Models 'vae'), $Downloads)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}
Ok '目录就绪'

# ---------------------------------------------------------------- 第三步：下载

function Get-Verified {
    # 参数名用 $Entry、局部量用 $info：PowerShell 变量名不区分大小写，
    # 若此处写 $item = Get-Item ... 会把参数 $Item 覆盖掉，导致 $Item.Size 变空值。
    param($Entry, [switch]$ForceRedownload)

    $dest = $Entry.Dest
    if ((Test-Path $dest) -and -not $ForceRedownload) {
        $info = Get-Item $dest
        if ($info.Length -eq $Entry.Size) {
            $hash = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
            if ($hash -eq $Entry.Sha) { Ok ("已存在且校验通过，跳过：" + (Split-Path $dest -Leaf)); return }
            Warn ("已存在且大小相符但哈希不符，删除后重新下载：" + (Split-Path $dest -Leaf))
            Remove-Item $dest -Force
        } else {
            Warn ("已存在但大小不符，继续下载：" + (Split-Path $dest -Leaf))
        }
    }

    # -f：服务器返回 4xx/5xx 时直接失败，不会把错误页当成文件存下来
    # -C -：断点续传，中断后重跑本脚本即可接着下
    # 不用 --retry-all-errors：它要求 curl 7.71 以上，而部分 Windows 自带版本低于此
    $curlArgs = @('-f', '-L', '-C', '-', '--retry', '10', '--retry-delay', '5',
                  '--retry-connrefused', '--connect-timeout', '30', '-o', $dest, $Entry.Url)
    if ($Proxy) { $curlArgs = @('-x', $Proxy) + $curlArgs }

    Say ("   下载 " + $Entry.Name)
    Say ("     " + $Entry.Url)
    & curl.exe @curlArgs
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path $dest) { Remove-Item $dest -Force -ErrorAction SilentlyContinue }
        Fail ("下载失败（curl 退出码 " + $LASTEXITCODE + "）。地址可能已变更，或网络不通。")
        Fail '消除原因后重新运行本脚本即可续传。'
        exit 1
    }

    $info = Get-Item $dest
    if ($info.Length -ne $Entry.Size) {
        Fail ("大小不符：实际 " + $info.Length + " 字节，预期 " + $Entry.Size + " 字节。")
        Remove-Item $dest -Force -ErrorAction SilentlyContinue
        Fail '已删除不完整的文件，重新运行可重新下载。'
        exit 1
    }
    $hash = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $Entry.Sha) {
        Fail ("SHA256 校验失败。" + [Environment]::NewLine + "     实际 " + $hash + [Environment]::NewLine + "     预期 " + $Entry.Sha)
        Fail '文件可能不完整或来源已变更，已停止。'
        exit 1
    }
    Ok ((Split-Path $dest -Leaf) + " 校验通过")
}

$totalGb = [math]::Round((($Items | ForEach-Object { $_.Size } | Measure-Object -Sum).Sum) / 1GB, 1)
Step ("第三步 / 下载并校验（共约 " + $totalGb + " GB，首次较慢）")
foreach ($entry in $Items) { Get-Verified -Entry $entry -ForceRedownload:$Force }

# ---------------------------------------------------------------- 第四步：解压落位

Step '第四步 / 解压推理程序与运行库'

$needExtract = $false
foreach ($item in $Items) {
    if ($item.Kind -eq 'zip') {
        $stamp = Join-Path $BinDir ('.done-' + (Split-Path $item.Dest -Leaf))
        if (-not (Test-Path $stamp)) { $needExtract = $true }
    }
}

if ($needExtract) {
    if (Test-Path $Temp) { Remove-Item $Temp -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Temp | Out-Null

    foreach ($item in ($Items | Where-Object { $_.Kind -eq 'zip' })) {
        $stamp = Join-Path $BinDir ('.done-' + (Split-Path $item.Dest -Leaf))
        if (Test-Path $stamp) { Ok ("已解压过，跳过：" + (Split-Path $item.Dest -Leaf)); continue }

        $sub = Join-Path $Temp ([IO.Path]::GetFileNameWithoutExtension($item.Dest))
        New-Item -ItemType Directory -Force -Path $sub | Out-Null
        Say ("   解压 " + (Split-Path $item.Dest -Leaf))
        Expand-Archive -Path $item.Dest -DestinationPath $sub -Force

        # 压缩包内部层级不固定，统一把所有文件平铺进 bin
        Get-ChildItem $sub -Recurse -File | ForEach-Object {
            $target = Join-Path $BinDir $_.Name
            Copy-Item $_.FullName $target -Force
        }
        New-Item -ItemType File -Force -Path $stamp | Out-Null
        Ok ((Split-Path $item.Dest -Leaf) + " 已解压到 bin\")
    }
    Remove-Item $Temp -Recurse -Force
} else {
    Ok '压缩包均已解压过，跳过'
}

$binCount = (Get-ChildItem $BinDir -File).Count
$binMb = [math]::Round((Get-ChildItem $BinDir -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Ok ("bin\ 现有 " + $binCount + " 个文件，共 " + $binMb + " MB")

# ---------------------------------------------------------------- 第五步：自检

Step '第五步 / 自检'

$env:PATH = "$BinDir;$env:PATH"
$cli = Join-Path $BinDir 'sd-cli.exe'
if (-not (Test-Path $cli)) {
    Fail 'bin\sd-cli.exe 不存在，解压环节可能有问题。'
    exit 1
}

Say '   查询推理后端能看到的设备（这一步能识别出 CUDA 运行库缺失）'
$devices = (& $cli --list-devices 2>&1 | Out-String).Trim()
Say '   设备列表：'
foreach ($line in ($devices -split "`r?`n")) {
    if ($line.Trim()) { Say ('     ' + $line.Trim()) }
}

if ($devices -match 'CUDA0') {
    Ok '显卡已被推理后端识别，CUDA 运行库齐全'
} else {
    Warn '没有看到 CUDA0。若是直连下载或压缩包不完整，重跑本脚本；'
    Warn '若文件都在，说明 CUDA 运行库未生效——这是本配置最常见的坑：'
    Warn '缺 cudart64_12.dll 时程序不报错，而是静默退回 CPU，速度慢十倍。'
    exit 1
}

Say ''
Say '   模型文件清单'
foreach ($item in ($Items | Where-Object { $_.Kind -eq 'file' })) {
    $exists = Test-Path $item.Dest
    $size = if ($exists) { [math]::Round((Get-Item $item.Dest).Length / 1GB, 2) } else { 0 }
    $flag = if ($exists) { '[有]' } else { '[缺]' }
    Say ("   $flag " + (Split-Path $item.Dest -Leaf) + "  " + $size + " GB")
}

Say ''
Say ('=' * 66)
Ok '安装完成'
Say ''
Say '下一步：'
Say '   cd gui-go; go run .   （需要 Go 1.27+，仓库根的 mise.toml 已钉版本）'
Say ''
Say '   打开界面后点「启动服务」，填提示词，点「生成」。首次出图需载入权重，'
Say '   约两分钟；之后连续出图不再重复加载。'
Say ('=' * 66)
