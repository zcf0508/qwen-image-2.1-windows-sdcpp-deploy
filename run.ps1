#requires -Version 5.1
<#
    用 stable-diffusion.cpp 跑 Qwen-Image-2.1 (Q4_K_M 混合精度 GGUF) 文生图与图像编辑。

    资源分配（12GB 显存）：扩散模型与 VAE 放显存，文本编码器（含视觉塔）留在系统内存。

    示例：
      .\run.ps1 -Prompt "一只橘猫在溪水边喝水" -Width 768 -Height 768
      .\run.ps1 -InitImage .\outputs\cat-water-768.png -PromptFile .\my-prompt.txt
#>
param(
    [string]$Prompt         = '一只可爱的猫在水边玩耍',
    [string]$PromptFile     = '',
    [string]$NegativePrompt = '',
    [int]$Width             = 768,
    [int]$Height            = 768,
    [int]$Steps             = 40,
    [double]$CfgScale       = 1.0,
    [string]$Sampler        = 'euler',
    [int]$Seed              = -1,
    [string]$Output         = '',
    [string]$InitImage      = '',
    [string]$VisionModel    = ''
)

$ErrorActionPreference = 'Stop'

$Root   = $PSScriptRoot
$BinDir = Join-Path $Root 'bin'
$Models = Join-Path $Root 'models'
$env:PATH = "$BinDir;$env:PATH"

# HQv3 混合精度版；该文件需先打补丁，见 README「量化版本选择」
$DiffusionModel = Join-Path $Models 'Qwen-Image-2.1-Q4_K_M-HQv3.gguf'
$Vae            = Join-Path $Models 'vae\qwen_image_2.1_vae_bf16.safetensors'

# 文本编码器按优先级自动选择：GGUF 版元数据完整，兼容性最好；
# safetensors 版（int8 convrot）作为备选。
$TextEncoder = @(
    (Join-Path $Models 'text_encoders\Qwen3VL-8B-Instruct-Q4_K_M.gguf')
    (Join-Path $Models 'text_encoders\qwen3vl_8b_int8_convrot.safetensors')
) | Where-Object { Test-Path $_ } | Select-Object -First 1

# 图像编辑需要视觉塔（mmproj），文生图不需要。
if ([string]::IsNullOrWhiteSpace($VisionModel)) {
    $VisionModel = @(
        (Join-Path $Models 'text_encoders\mmproj-Qwen3VL-8B-Instruct-F16.gguf')
        (Join-Path $Models 'text_encoders\mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}

foreach ($f in @($DiffusionModel, $TextEncoder, $Vae)) {
    if (-not (Test-Path $f)) { throw "缺少模型文件：$f" }
}

$IsEdit = -not [string]::IsNullOrWhiteSpace($InitImage)
if ($IsEdit) {
    if (-not (Test-Path $InitImage)) { throw "输入图片不存在：$InitImage" }
    if ([string]::IsNullOrWhiteSpace($VisionModel)) {
        throw "图像编辑需要视觉塔：请把 mmproj-Qwen3VL-8B-Instruct-F16.gguf 放到 models\text_encoders\ 下，或用 -VisionModel 指定"
    }
}

if ([string]::IsNullOrWhiteSpace($Output)) {
    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Output = Join-Path $Root "outputs\qwen-${Width}x${Height}-$stamp.png"
}
New-Item -ItemType Directory -Force -Path (Split-Path $Output) | Out-Null

$SdArgs = @(
    '--diffusion-model',  $DiffusionModel
    '--vae',              $Vae
    '--llm',              $TextEncoder
    '--backend',          'te=cpu'
    '--diffusion-fa'
    '--sampling-method',  $Sampler
    '--cfg-scale',        $CfgScale
    '--steps',            $Steps
    '--width',            $Width
    '--height',           $Height
    '--seed',             $Seed
    '--output',           $Output
)
if ($IsEdit) {
    $SdArgs += @('--ref-image',  (Resolve-Path $InitImage).Path)
    $SdArgs += @('--llm_vision', $VisionModel)
}
if (-not [string]::IsNullOrWhiteSpace($PromptFile)) {
    if (-not (Test-Path $PromptFile)) { throw "提示词文件不存在：$PromptFile" }
    $Prompt   = (Get-Content -Path $PromptFile -Raw -Encoding UTF8).Trim()
    $SdArgs  += @('--prompt-file', (Resolve-Path $PromptFile).Path)
}
else {
    $SdArgs += @('--prompt', $Prompt)
}
if (-not [string]::IsNullOrWhiteSpace($NegativePrompt)) {
    $SdArgs += @('--negative-prompt', $NegativePrompt)
}

Write-Host "prompt : $Prompt"
Write-Host "encoder: $TextEncoder"
if ($IsEdit) {
    Write-Host "edit   : $InitImage"
    Write-Host "vision : $VisionModel"
}
Write-Host "size   : ${Width}x${Height}  steps=$Steps  cfg=$CfgScale  sampler=$Sampler  seed=$Seed"
Write-Host "output : $Output"

& (Join-Path $BinDir 'sd-cli.exe') @SdArgs
if ($LASTEXITCODE -ne 0) { throw "sd-cli 退出码 $LASTEXITCODE" }

$Item = Get-Item $Output
Write-Host ("done   : {0}  {1:N0} bytes" -f $Item.FullName, $Item.Length)
