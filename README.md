# Qwen-Image-2.1 本地部署

在 12GB 显存以上的 NVIDIA 显卡上本地跑 Qwen-Image-2.1，支持文生图与图像编辑，基于 stable-diffusion.cpp，不依赖 ComfyUI。

工具链：`sd-server.exe` / `sd-cli.exe` 提供推理后端，外加一个图形界面和两个命令行入口。模型权重共约 12 GB，全部由安装脚本下载并逐一校验 SHA256。

## 快速开始

三步，全程约半小时，大头是下载。

```powershell
# 1. 准备环境并下载依赖，首次约 13 GB
.\setup.ps1

# 2. 启动图形界面（Go 1.27+，仓库根的 mise.toml 已钉版本）
cd gui-go
go run .

# 3. 填写画面描述 → 点「生成图像」（服务会按需自动启动）
```

需要代理时加参数，脚本会把它同时用于所有下载：

```powershell
.\setup.ps1 -Proxy http://127.0.0.1:7890
```

安装脚本可重复执行：已下载且校验通过的文件会跳过，下载中断后重跑即可续传。想同时备一份速度更快的普通量化，加 `-WithFallback`。

## 目录结构

```
<仓库根目录>\
├── setup.ps1                       安装脚本：环境检查、依赖下载校验、解压落位、设备自检
├── gui-go\                         图形界面（Go + MyGo 原生 UI，推荐入口）
├── launcher.py                     命令行启动器（uv run）
├── run.ps1                         PowerShell 生成脚本
├── patch_gguf_img_in.py            维护用：修正 ComfyUI-GGUF 导出模型的形状声明
├── bin\                            sd-cli.exe / sd-server.exe + CUDA 运行库（由 setup.ps1 下载）
├── models\                         权重（由 setup.ps1 下载，共约 12 GB）
│   ├── Qwen-Image-2.1-Q4_K_M-HQv3.gguf                 扩散模型（在用）5960 MB → 显存
│   ├── qwen-image-2.1-Q4_K_M.gguf                     扩散模型（备选，需 -WithFallback）4605 MB → 显存
│   ├── text_encoders\
│   │   ├── Qwen3VL-8B-Instruct-Q4_K_M.gguf            文本编码器 4795 MB → 内存
│   │   └── mmproj-Qwen3VL-8B-Instruct-F16.gguf        视觉塔    1105 MB → 内存（仅编辑）
│   └── vae\qwen_image_2.1_vae_bf16.safetensors        VAE        644 MB → 显存
├── outputs\                        生成结果
├── downloads\                      安装包缓存（可删）
└── README.md
```

## 量化版本选择

在用 HQv3 混合精度版 `Qwen-Image-2.1-Q4_K_M-HQv3.gguf`（来自 realrebelai）。它的注意力层与 MLP 输出层保持 q8_0，其余为 q4_K / q5_K / q6_K，同等体积下比普通 Q4_K_M 多留一部分注意力精度。

本机实测（1024 × 1024、40 步、cfg 1.0、euler，模型已载入）：

| 版本 | 参数显存 | 采样 | 解码 | 单张总计 |
| --- | --- | --- | --- | --- |
| 普通 Q4_K_M | 5036 MiB | 70.23 秒 | 5.74 秒 | 80.32 秒 |
| HQv3 | 5683 MiB | 78.77 秒 | 5.72 秒 | 88.87 秒 |

HQv3 多占 647 MiB 显存，1024 × 1024 的解码因此从整体解码退为分块。分块这一步本身不改变画质：1024 在 128 网格上，实测无接缝。1280 以上的尺寸本来就在分块，不受影响。

### 为什么这个文件需要打补丁

该文件由 ComfyUI-GGUF 转换器导出，它把 `img_in.weight` 从 `[64, 4096]` 重排为 `[256, 1024]`（输出维除以 4、输入维乘以 4），并把原始形状记在元数据 `comfy.gguf.orig_shape.img_in.weight` 里。

stable-diffusion.cpp 恰好用这个张量的形状推断网络规模（`src/model/diffusion/qwen_image_2_1.hpp`）：

```cpp
config.in_channels = w->ne[0];
config.hidden_size = w->ne[1];
```

被重排后它会按 hidden = 1024 去建一个 7B 网络，与文件里 4096 维的张量全线不符，报 `model metadata validation failed` 并拒绝加载。

`patch_gguf_img_in.py` 把这两个 uint64 改回 `[64, 4096]`。元素总数 262144 改前改后一致，所以只是标签改写：实测只有 3 个字节发生变化，张量数据零移动，文件大小与张量表自洽性都不变。改前会把文件头备份为 `Qwen-Image-2.1-Q4_K_M-HQv3.gguf.header.bak`（与模型同目录），要还原直接覆盖回去。脚本可重复执行，形状已经是目标值时不做任何改动。

重新下载这个模型后必须重跑一次：

```powershell
uv run --no-project patch_gguf_img_in.py
```

### 画质对比

同种子、同提示词、同参数对比，HQv3 在细节上略好，但差距有限：普通版皮肤纹理同样扎实，但有一根手指比例失调、键盘黑键排列错乱；HQv3 不再有比例失调的长指、背景手更清楚，代价是出现了缺指。手部结构仍是 7B 加 4 比特量化的能力边界，换量化版本解决不了这个层面。

想换回普通量化：把 `gui-go\server.go`、`launcher.py`、`run.ps1` 里的模型文件名改回 `qwen-image-2.1-Q4_K_M.gguf`，那个文件仍在原处。

## 图形界面 gui-go

```powershell
cd gui-go
go run .          # 开发时可用 go tool mygo dev，改代码自动重启
```

Go + [MyGo](https://mygo.egoist.dev/) 原生界面：无 webview、无 cgo，Windows 上用 Direct3D 11 绘制，单个小体积可执行文件。`go tool mygo build` 产出独立 exe 与 NSIS 安装包（在 `gui-go\build\`）。`go test` 在无窗口环境渲染视图做回归测试。界面分四块：

| 区域 | 内容 |
| --- | --- |
| 顶部状态栏 | 服务状态（颜色区分未启动 / 加载中 / 就绪）、显存与内存占用，每 2 秒刷新 |
| 左栏 | 文生图 / 图像编辑切换、上下文相关的参考图控件、画面描述、尺寸预设和参数；参数错误会就地提示 |
| 右侧 | 随窗口自适应的结果预览，可直接打开图片、另存为或打开输出目录 |
| 底部 | 自动换行的服务端运行记录 |

按 `Ctrl+Enter` 可直接生成。服务加载和生成期间，按钮文案与进度条会同步反馈当前状态；启动服务的请求不会与生成请求并发。图像编辑的参考图区域只在编辑模式显示，移动和移除按钮会按当前选择自动启用。

### 尺寸预设

七个宽高比各占一格，格内画出该比例的缩略矩形，点一下即切换宽高。勾选「高清档」后整条换成另一套更大的尺寸。

标准档：

| 比例 | 尺寸 | 比例 | 尺寸 |
| --- | --- | --- | --- |
| 1:1 | 1024 × 1024 | 2:3 | 768 × 1152 |
| 4:3 | 1024 × 768 | 16:9 | 1152 × 640 |
| 3:4 | 768 × 1024 | 9:16 | 640 × 1152 |
| 3:2 | 1152 × 768 | | |

高清档（边长约 1.5 倍，像素量 1.4–2.4 MP，单张约 2.3–3.6 分钟）：

| 比例 | 尺寸 | 比例 | 尺寸 |
| --- | --- | --- | --- |
| 1:1 | 1536 × 1536 | 2:3 | 1024 × 1536 |
| 4:3 | 1536 × 1152 | 16:9 | 2048 × 1152 |
| 3:4 | 1152 × 1536 | 9:16 | 1152 × 2048 |
| 3:2 | 1536 × 1024 | | |

高清档的 16:9 取 2048 × 1152，比例正好是 1.778；标准档受尺寸所限只能取 1152 × 640（1.80）。

七种比例统一按 128 的网格取值。这条约束来自实测：VAE 分块解码的接缝会落在瓦片边界上，当尺寸不是 128 的整数倍时，接缝处会出现明显的亮度台阶。本机实测，1280 × 864（864 ÷ 128 = 6.75）在接缝处的行均值跳变达 9.39 个灰度级，是画面里最强的异常，位置恰好对应接缝；而 1280 × 896、1152 × 768、1024 × 768、1024 × 1024 这些 128 整数倍尺寸，同一位置的跳变只有 0.15 到 0.28，等于噪声本底。所以只要尺寸落在 128 网格上，分块解码就不会留条带，不需要为避开分块而压低分辨率。

分辨率高的档位不只是更清楚。本机同种子对比，2048 × 1152 相比 1152 × 768，键盘键帽的排列从混乱融合变成规整可辨的完整配列，锐度与材质层次也明显提升。追求画质时优先用高清档。

分块解码常开。SD 服务端不像 `sd-cli` 那样在解码失败后自动降级，尺寸超过约 1 MP 就必须显式开启，否则整张直接判失败（实测 1536 × 1024 不开分块必失败）。界面提交的每个任务都带上 `vae_tiling_params.enabled`，代价接近于零：1024 × 1024 下分块解码 5.72 秒，整体解码 5.74 秒。

手动输入尺寸时：宽和高必须是 128 的倍数。引擎本身只要求 32 的倍数，但分块解码常开，非 128 倍数会在接缝处出现亮度台阶，界面直接拦截并给出常用可用值。

### 分辨率与耗时

单张耗时（40 步、cfg 1.0、euler、模型已载入，同机实测）：

| 分辨率 | 像素 | 采样 | 解码 | 单张总计 |
| --- | --- | --- | --- | --- |
| 1024 × 1024 | 1.05 MP | 78.77 秒 | 5.72 秒 | 1.5 分钟 |
| 1536 × 1024 | 1.57 MP | 121.04 秒 | 9.64 秒 | 2.3 分钟 |
| 1280 × 1280 | 1.64 MP | 114.43 秒 | 8.44 秒 | 2.1 分钟 |
| 2048 × 1152 | 2.36 MP | 192.85 秒 | 12.57 秒 | 3.6 分钟 |
| 1408 × 2048 | 2.88 MP | 267.06 秒 | 15.64 秒 | 4.9 分钟 |
| 2048 × 2048 | 4.19 MP | 415.27 秒 | 27.17 秒 | 7.5 分钟 |

除 1280 与 2048 × 2048 两行用普通 Q4_K_M 实测（采样约快 12%）外，其余均为 HQv3 模型实测。1536 × 1536 未单独实测，可参照同像素量的 2048 × 1152。

显存瓶颈在 VAE 解码而非采样。2048 即使跑 40 步，采样也只占总时长的 93%，而一次性解码要 40 GB 显存预算，远超 12 GB 卡。分块解码把这一步拆成瓦片，2048 × 1152 的解码只要 12.57 秒、1408 × 2048 只要 15.64 秒。

### 参考图与图像编辑

「图像编辑」模式可以选**多张**参考图。界面上是一个有序列表，行首序号就是提示词里的「图一、图二」；用「上移 / 下移」调整先后，用「移除」删掉某一张。选的时候按住 Ctrl 或 Shift 可多选，重复选同一张会被忽略。提交时按列表顺序逐张编码进数组，日志里会打印「参考图顺序：图1 xxx.png，图2 yyy.png」便于回溯。

图片也可以直接从资源管理器**拖进窗口**：拖到窗口任意位置即可，多个文件按拖入顺序追加到列表，拖完自动切到「图像编辑」模式；非图片文件和目录会被忽略，日志里给出提示。实现上没有引入 `tkinterdnd2` 这类第三方包，而是用 `ctypes` 接管 Tk 顶层窗口的窗口过程，收系统投递的 `WM_DROPFILES`（配 `DragAcceptFiles` 注册）。

**但顺序不是模型一定会遵守的契约。** 实测用两张内容差异明显的图（水边的猫 / 夜晚书桌），提示词「把图一中的动物放到图二的键盘上」，跑两次只对调参考图先后：

| 顺序 | 结果 |
| --- | --- |
| 图一=猫，图二=书桌 | 猫趴在书桌的键盘上，光影与场景取自书桌那张 |
| 图一=书桌，图二=猫 | 同样是猫站在键盘上，构图几乎一致，只是猫爪下多出一片水渍 |

两次都能完成「主体取自一张、场景取自另一张」的组合，但结果几乎不受先后影响——**模型看起来是按内容识别参考图的**（找出哪张有动物、哪张有键盘），而不是按序号。所以提示词里写清内容（「把猫放到键盘上」）比只写「图一图二」更可靠；只有两张图内容相近、必须靠序号区分时，才需要依赖顺序，而那种情况没有验证过。

**多张参考图代价明显。** 1024 × 1024、40 步、两张参考图，实测单张 515 到 576 秒；单张参考图同尺寸约三到四分钟。参考图越多、分辨率越高，耗时增长越快，显存与内存占用也会上升（两张 1024 参考图时实测总占用 VRAM 6327 MB / RAM 5408 MB）。

编辑模式需要视觉塔，但用户无需手动控制。切换到「图像编辑」时界面会自动启用编辑能力；即使服务已经以文生图模式启动，提交编辑任务时也会自动重启并加载视觉塔。服务端日志出现 `image.cpp - EDIT mode` 才说明真的进了编辑流程。

### 资源管理

后端是常驻的 `sd-server.exe` 进程，界面负责它的完整生命周期：

- **加载一次，反复出图。** 模型只在首次出图时载入，之后常驻，连续生成不再重复读盘。这是相比每次单跑 `sd-cli` 最主要的收益。
- **随时可释放。** 点「停止服务」立即结束进程，显存与内存同步释放。
- **看得见占用。** 状态栏实时显示显存与内存，出图前后能直接看出模型吃掉了多少。
- **退出不残留。** 关闭窗口会结束服务进程；即使窗口被任务管理器强杀或异常崩溃，子进程也会被一并结束（通过 Windows Job 对象绑定实现，已实测验证）。若曾在服务运行中点「生成」后又直接杀进程，不会再留下占用 9 GB 内存的孤儿进程。
- **编辑模式按需加载。** 初始的文生图模式不加载视觉塔；切换到图像编辑后自动启用。视觉塔会多占约 1.1 GB 内存；若服务已启动但未加载，提交编辑任务时会自动重启服务并加载。

### 关于取消

服务端的 capabilities 中 `cancel_generating = false`，即**不支持中断正在生成的画面**，只能取消排队中的任务。因此界面上点「取消」会提示改用「强制停止服务」——后者直接结束进程、立刻释放资源，代价是当前这张图作废。

## 命令行用法

### launcher.py

```powershell
cd <仓库根目录>

# 交互式向导：选模式 → 写提示词 → 选参考图 → 设参数
uv run --no-project launcher.py

# 直接文生图
uv run --no-project launcher.py -p "一只橘猫在溪水边喝水"

# 图像编辑：传 -i 即为编辑模式
uv run --no-project launcher.py -i outputs\cat-water-768.png -p "把猫换成小狗"
```

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `-p, --prompt` | 提示词；提供后不再进入交互向导 | 空 |
| `-i, --image` | 参考图路径，提供即为图像编辑模式 | 空 |
| `-n, --negative-prompt` | 负面提示词 | 空 |
| `-W / -H` | 宽 / 高，必须是 32 的倍数 | 768 / 768 |
| `-s, --steps` | 采样步数 | 40 |
| `--cfg-scale` | 引导强度 | 1.0 |
| `--sampler` | 采样器 | euler |
| `--seed` | 随机种子，小于 0 为随机 | -1 |
| `-o, --output` | 输出路径 | outputs\qwen-<尺寸>-<时间>.png |
| `--dry-run` | 只打印命令，不实际运行 | — |

### run.ps1

不依赖 Python，直接调用 `sd-cli`，每次运行都会重新加载模型。

```powershell
cd <仓库根目录>
.\run.ps1 -Prompt "一只橘猫在溪水边喝水" -Width 768 -Height 768 -Steps 20

# 图像编辑
.\run.ps1 -InitImage .\outputs\cat-water-768.png `
          -PromptFile .\my-prompt.txt `
          -Output .\outputs\dog-water-768.png
```

参数与 `launcher.py` 基本一致，另有 `-PromptFile` 从 UTF-8 文本文件读提示词、`-VisionModel` 手动指定视觉塔。

## 资源占用与性能

脚本只固定 `--backend te=cpu`（文本编码器走 CPU），其余交给 sd.cpp 的 auto-fit 自动放置。HQv3 模型、1024 × 1024 实测：

```
DiT          params   5683 MiB, compute reserve  2048 MiB -> compute CUDA0, params CUDA0
Conditioner  params   4789 MiB, compute reserve  2048 MiB -> compute CPU,   params cpu
VAE          params    644 MiB, compute reserve  1024 MiB -> compute CUDA0, params CUDA0

total params memory size = 10629.76MB (VRAM 6327.42MB, RAM 4302.33MB):
    text_encoders 4302.33MB(RAM), diffusion_model 5683.05MB(VRAM), vae 644.38MB(VRAM)
```

图像编辑额外载入视觉塔，系统内存再增约 1.1 GB，显存占用不变。

耗时（本机实测，模型已常驻；euler 采样）：

| 场景 | 参数 | 耗时 |
| --- | --- | --- |
| 服务进程 HTTP 就绪 | — | 约 3 秒 |
| 冷启动首次出图（含载入权重） | — | 约 140 秒 |
| 文生图 768×768 | 20 步 + 引导 6.0 | 约 50 秒 |
| 图像编辑 640×800 | 20 步 + 引导 6.0 | 约 184 秒 |
| 图像编辑 640×800 | 40 步 + 引导 1.0 | 约 141 秒 |

图像编辑慢得多，因为参考图要先走一次视觉编码（文本编码阶段 50～102 秒，取决于是否启用 CFG；文生图仅约 5 秒）。

引导设为 1 会同时省掉两处开销：采样每步只跑一次前向（3.43 秒/步 → 1.85 秒/步），文本编码也不再需要为负面提示词再编一遍（约 102 秒 → 约 50 秒）。因此步数翻倍后总耗时反而少约 23%。官方 Qwen 仓库的默认值是 40 步，ComfyUI 官方模板是 25 步 + cfg 1，两者都不用 CFG；本仓默认已按此调整为 40 步 + 引导 1。

服务端限制（来自 `/sdcpp/v1/capabilities`）：宽高 64–4096，批量最多 8 张，队列最多 64 个任务。

## 环境要求

- **仅支持 Windows**，需要 NVIDIA 显卡。显存 12 GB 起（开发与测试平台为 RTX 4070 12GB，驱动 610.47）；显存不足时安装脚本会提示，小尺寸仍可能跑，但高清档必然失败
- 系统内存 32 GB。权重常驻时文本编码器约占 4.3 GB，加上日常桌面程序约 20 GB 基线
- 磁盘可用空间 25 GB 以上（权重 12 GB、推理程序与运行库 1.1 GB、安装包缓存 0.9 GB）
- 需要 [uv](https://docs.astral.sh/uv/)。安装脚本会检查，缺失时给出安装命令；Python 由 uv 管理，无需自己安装
- 首次需下载约 13 GB。国内网络通常需要代理，用 `.\setup.ps1 -Proxy http://127.0.0.1:7890`

## 组件来源

| 组件 | 来源 |
| --- | --- |
| 扩散模型 GGUF（普通 Q4_K_M，备选） | https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF |
| 扩散模型 GGUF（Q4_K_M-HQv3，在用） | https://huggingface.co/zcf0508/qwen-image-2.1-hqv3-sdcpp-fixed （已修正 sd.cpp 兼容性） |
| 扩散模型量化原始出处 | https://huggingface.co/realrebelai/Qwen-Image-2.1_GGUFs |
| 文本编码器 / 视觉塔 GGUF | https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct-GGUF |
| VAE | https://huggingface.co/abenzerps/Qwen-Image-2.1-Uncensored-GGUF |
| 推理程序 | https://github.com/leejet/stable-diffusion.cpp |
| 上游基座模型 | https://huggingface.co/Qwen/Qwen-Image-2.1 |

## 注意事项

**CUDA 运行库必须单独安装。** stable-diffusion.cpp 的 Windows CUDA 包只带 `ggml-cuda.dll`，它依赖 `cudart64_12.dll`、`cublas64_12.dll`、`cublasLt64_12.dll`，这三个在单独的 `cudart-sd-bin-win-cu12-x64.zip` 里。缺了它程序不会报错，而是静默退回 CPU——表现就是显存占用为零、速度慢十倍。可用 `bin\sd-cli.exe --list-devices` 确认是否列出 `CUDA0`。

**文本编码器用 GGUF，不要用 safetensors。** ComfyUI 打包的 `qwen3vl_8b_int8_convrot.safetensors` 在 sd.cpp 中会被错误推断 hidden size（识别成词表大小 151936），随后在量化 scale 断言处崩溃。GGUF 自带完整元数据，没有这个问题。

**不要加 `--offload-to-cpu`。** 它等价于 `--params-backend '*=cpu'`，会把全部约 10.6 GB 权重钉在系统内存并禁用 auto-fit，导致扩散模型无法上显存。仅在显存确实不够时才用。

**编辑模式必须有视觉塔。** 缺少 mmproj 时 sd.cpp 只打印 `no vision weights detected, vision disabled` 然后照常执行，但参考图根本不会被读取，等于白跑一次文生图。日志里出现 `image.cpp - EDIT mode` 才说明真的进了编辑流程。

**编辑模式 VAE 解码可能触发显存不足。** 实测出现过 `model manager cannot make enough memory available on CUDA0`，程序自动降级为空间分块解码（spatial tiling）并正常出图，属预期行为，日志中的重试提示不必处理。

## 许可与致谢

- 上游基座模型 [Qwen/Qwen-Image-2.1](https://huggingface.co/Qwen/Qwen-Image-2.1) 适用 **Qwen Research License**：默认供研究与评估使用，商用需另行取得授权。本仓库的脚本与文档不改变该许可，下载并使用模型即表示你接受其条款。
- 量化权重出自 realrebelai 的 [Qwen-Image-2.1_GGUFs](https://huggingface.co/realrebelai/Qwen-Image-2.1_GGUFs)（HQv3 混合精度方案）。本仓使用的 HQv3 文件只修正了一处元数据，使 stable-diffusion.cpp 能正确推断网络规模，量化本身未做任何改动；修正过程与原理见 [修正仓库](https://huggingface.co/zcf0508/qwen-image-2.1-hqv3-sdcpp-fixed)。
- 推理后端为 [leejet/stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)，版本钉死在 `master-889-c678dfe`。
- 本仓库的脚本与文档（`setup.ps1`、`gui-go`、`launcher.py`、`run.ps1`、`patch_gguf_img_in.py`）可自由使用与修改。

## 维护

**降低显存压力的备选手段**，依次尝试：把尺寸降到 512、步数降到 12、追加 `--vae-tiling`，或改用 `--params-backend diffusion=disk` 让扩散模型按需从磁盘加载权重。

**可安全删除的文件**（删前确认不再需要）：

| 路径 | 大小 | 说明 |
| --- | --- | --- |
| `models\text_encoders\qwen3vl_8b_int8_convrot.safetensors` | 8.9 GB | 未使用的备选编码器 |
| `downloads\` | 约 1.3 GB | 安装包与解压暂存 |

`downloads\` 下的两个压缩包建议保留，重装时可省去重新下载。它们由 `setup.ps1` 按上游资产名保存，文件名与 GitHub 发布页一致，所以对照文件名就能看出用的是哪个版本。
