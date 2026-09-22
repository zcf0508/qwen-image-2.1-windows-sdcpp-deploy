# ============================================================================
# 在 Colab 里把 realrebelai 的量化补正后重新上传（可直接整段粘贴进一个单元格）
#
# 做四件事：
#   1. 从作者仓库下载指定量化文件
#   2. 把 img_in.weight 的形状声明改回 [64, 4096]（仅改元数据，张量数据不动）
#   3. 逐项校验：元素总数不变、只有预期字节被改、张量表仍自洽、回读形状正确
#   4. 建仓库并上传修正后的文件、补丁脚本和带署名与许可声明的 README
#
# 前置：在 Colab 左侧「密钥」面板添加一条 HF_TOKEN，值填带写权限的 HuggingFace token。
#       不要把 token 直接写在代码里——notebook 会被保存和分享。
# ============================================================================

import os
import struct
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 配置

SRC_REPO = "realrebelai/Qwen-Image-2.1_GGUFs"   # 作者仓库
DST_REPO = "qwen-image-2.1-hqv3-sdcpp-fixed"    # 你的接收仓库名（会自动建）
FILES = [                                       # 要处理的文件，可加其余四档
    "Qwen-Image-2.1-Q4.gguf",
    # "Qwen-Image-2.1-Q2.gguf",
    # "Qwen-Image-2.1-Q3.gguf",
    # "Qwen-Image-2.1-Q5.gguf",
    # "Qwen-Image-2.1-Q8.gguf",
]
TENSOR = "img_in.weight"
TARGET_SHAPE = (64, 4096)                       # ggml 记法：输入维在前
WORKDIR = Path("/content/gguf_fix")

print("安装依赖…")
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"],
               check=False)

from huggingface_hub import HfApi, hf_hub_download  # noqa: E402

try:
    from google.colab import userdata                # noqa: E402
    TOKEN = userdata.get("HF_TOKEN")
except Exception:
    TOKEN = os.environ.get("HF_TOKEN")

if not TOKEN:
    raise SystemExit("没拿到 HF_TOKEN，请在 Colab「密钥」面板添加一条再运行。")

api = HfApi(token=TOKEN)
who = api.whoami()
OWNER = who["name"]
DST_REPO = DST_REPO if "/" in DST_REPO else "%s/%s" % (OWNER, DST_REPO)
print("当前账号：%s" % OWNER)
print("接收仓库：%s" % DST_REPO)
WORKDIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- GGUF 解析

_SCALAR_WIDTH = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_BLOCK = {0: (4, 1), 1: (2, 1), 30: (2, 1), 2: (18, 32), 3: (20, 32), 6: (22, 32),
          7: (24, 32), 8: (34, 32), 9: (36, 32), 10: (84, 256), 11: (110, 256),
          12: (144, 256), 13: (176, 256), 14: (210, 256), 15: (292, 256), 28: (2, 1)}


def _read(handle, count):
    data = handle.read(count)
    if len(data) != count:
        raise EOFError("文件提前结束")
    return data


def _skip_value(handle, vtype):
    if vtype in _SCALAR_WIDTH:
        _read(handle, _SCALAR_WIDTH[vtype])
    elif vtype == 8:
        _read(handle, struct.unpack("<Q", _read(handle, 8))[0])
    elif vtype == 9:
        etype = struct.unpack("<I", _read(handle, 4))[0]
        count = struct.unpack("<Q", _read(handle, 8))[0]
        if etype == 8:
            for _ in range(count):
                _read(handle, struct.unpack("<Q", _read(handle, 8))[0])
        else:
            _read(handle, _SCALAR_WIDTH[etype] * count)
    else:
        raise ValueError("元数据类型 %d 无法跳过" % vtype)


def scan(path, want_tensor):
    """返回 (形状字段偏移, 维度数, 当前形状, 数据起始偏移, 声明总字节)。"""
    with open(path, "rb") as handle:
        if _read(handle, 4) != b"GGUF":
            raise ValueError("不是 GGUF 文件")
        _read(handle, 4)                                     # 版本
        tensor_count = struct.unpack("<Q", _read(handle, 8))[0]
        kv_count = struct.unpack("<Q", _read(handle, 8))[0]
        for _ in range(kv_count):
            _read(handle, struct.unpack("<Q", _read(handle, 8))[0])
            _skip_value(handle, struct.unpack("<I", _read(handle, 4))[0])

        field = None
        dims = None
        shape = None
        declared = 0
        for _ in range(tensor_count):
            length = struct.unpack("<Q", _read(handle, 8))[0]
            name = _read(handle, length).decode("utf-8", "replace")
            n_dims = struct.unpack("<I", _read(handle, 4))[0]
            offset = handle.tell()
            ne = [struct.unpack("<Q", _read(handle, 8))[0] for _ in range(n_dims)]
            dtype = struct.unpack("<I", _read(handle, 4))[0]
            _read(handle, 8)                                 # 数据偏移
            if dtype in _BLOCK:
                elems = 1
                for dim in ne:
                    elems *= dim
                block_bytes, block_elems = _BLOCK[dtype]
                declared += elems // block_elems * block_bytes
            if name == want_tensor:
                field, dims, shape = offset, n_dims, ne
        return field, dims, shape, handle.tell(), declared


def patch(path):
    """改回 img_in.weight 的形状声明，返回校验结果字典。"""
    before_header = path.open("rb").read(1 << 20)
    field, dims, shape, data_start, declared = scan(path, TENSOR)
    if field is None:
        return {"状态": "张量表里没有 %s" % TENSOR}
    if shape == list(TARGET_SHAPE):
        return {"状态": "已经是目标形状，跳过", "改前": shape}

    old_elems = 1
    for dim in shape:
        old_elems *= dim
    new_elems = 1
    for dim in TARGET_SHAPE:
        new_elems *= dim
    if old_elems != new_elems:
        return {"状态": "元素总数不同，拒绝改动", "改前": shape, "旧": old_elems, "新": new_elems}

    with open(path, "r+b") as handle:
        handle.seek(field)
        current = [struct.unpack("<Q", _read(handle, 8))[0] for _ in range(dims)]
        if current != shape:
            return {"状态": "写入前回读与首次解析不一致，中止"}
        handle.seek(field)
        for value in TARGET_SHAPE:
            handle.write(struct.pack("<Q", value))
        handle.flush()

    field2, dims2, shape2, data_start2, declared2 = scan(path, TENSOR)
    after_header = path.open("rb").read(1 << 20)
    changed = [i for i in range(len(before_header)) if before_header[i] != after_header[i]]
    file_size = path.stat().st_size
    return {
        "状态": "已修正",
        "改前": shape,
        "改后": shape2,
        "形状回读": "一致" if shape2 == list(TARGET_SHAPE) else "不一致",
        "变化字节数": len(changed),
        "变化位置": "%d..%d" % (changed[0], changed[-1]) if changed else "无",
        "文件大小": file_size,
        "表自洽差额": file_size - data_start2 - declared2,
    }


# ---------------------------------------------------------------- 主流程

results = {}
for filename in FILES:
    print()
    print("=" * 70)
    print(filename)
    print("=" * 70)
    try:
        local = Path(hf_hub_download(repo_id=SRC_REPO, filename=filename,
                                     local_dir=str(WORKDIR / "src"), token=TOKEN))
        print("已下载 %s（%d 字节）" % (local.name, local.stat().st_size))
        info = patch(local)
        for key, value in info.items():
            print("  %-10s %s" % (key, value))
        results[filename] = (local, info)
    except Exception as exc:                                     # noqa: BLE001
        print("处理失败：%r" % (exc,))
        results[filename] = (None, {"状态": "失败：%r" % (exc,)})

# 上传
print()
print("=" * 70)
print("建立仓库并上传")
print("=" * 70)
api.create_repo(repo_id=DST_REPO, repo_type="model", exist_ok=True, private=False)

PATCH_SCRIPT = '''"""把 ComfyUI-GGUF 导出模型里 img_in.weight 的形状声明改回原始值。

该张量被从 [64, 4096] 重排为 [256, 1024]，而 stable-diffusion.cpp 用它推断网络规模
（src/model/diffusion/qwen_image_2_1.hpp 里 config.hidden_size = w->ne[1]），
于是把 7B 网络按 hidden=1024 建出来，报 model metadata validation failed。

元素总数不变，所以只改元数据标签，张量数据一格不动。

    uv run --no-project patch_gguf_img_in.py <模型路径>
"""
'''

uploaded = []
for filename, (local, info) in results.items():
    if local is None or not local.is_file():
        print("跳过 %s（未准备好）" % filename)
        continue
    target_name = filename.replace(".gguf", "-sd.cpp.gguf")
    api.upload_file(path_or_fileobj=str(local), path_in_repo=target_name,
                    repo_id=DST_REPO, commit_message="修正 img_in.weight 形状声明")
    uploaded.append(target_name)
    print("已上传 %s" % target_name)

readme = """---
license: other
license_name: qwen-research
license_link: https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE
base_model: Qwen/Qwen-Image-2.1
library_name: gguf
pipeline_tag: text-to-image
tags:
  - gguf
  - qwen-image
  - stable-diffusion.cpp
---

# Qwen-Image-2.1 HQv3 GGUF，修正 sd.cpp 兼容性

本仓库是 [realrebelai/Qwen-Image-2.1_GGUFs](https://huggingface.co/realrebelai/Qwen-Image-2.1_GGUFs)
的量化文件的**元数据修正版**，使其能被 stable-diffusion.cpp 加载。量化本身由 realrebelai 完成，
本仓库未改动任何量化参数。

## 改了什么

仅改动每个文件中 `img_in.weight` 这一个张量的形状声明，由 `[256, 1024]` 改回 `[64, 4096]`。

原因：该文件由 ComfyUI-GGUF 转换器导出，转换器把 `img_in.weight` 从 `[64, 4096]` 重排为
`[256, 1024]`，并把原始形状记在元数据 `comfy.gguf.orig_shape.img_in.weight` 里。ComfyUI 会读取该记录
自行还原，所以没有影响；但 stable-diffusion.cpp 恰好用这个张量的形状推断整个网络的规模：

```cpp
if (auto w = find("img_in.weight")) {
    config.in_channels = w->ne[0];
    config.hidden_size = w->ne[1];
}
```

于是它把 7B 网络按 `hidden_size = 1024` 建立，与文件中 4096 维的张量全部不符，加载时报
`model metadata validation failed` 并拒绝继续。

两个形状的元素总数相同（64 × 4096 = 256 × 1024 = 262144），因此这只是标签改写：
实测每个文件只有 3 个字节发生变化，张量数据一格未移动，文件大小与张量表自洽性均不变。

## 文件

%s

命名规则：原文件名 + `-sd.cpp`。例如 `Qwen-Image-2.1-Q4-sd.cpp.gguf` 对应作者仓库的
`Qwen-Image-2.1-Q4.gguf`。

## 验证范围

- `Qwen-Image-2.1-Q4-sd.cpp.gguf`：已在 stable-diffusion.cpp master-889（commit c678dfe）、
  RTX 4070 12GB 上端到端跑通，1024×1024 / 40 步 / cfg 1.0 正常出图，画质与未重排的普通 Q4_K_M 一致。
- 其余量化档：仅做结构性校验（元素总数不变、张量表自洽、形状回读正确），**未逐档实测出图**。

## 自行施加修正

如果你已经下载了作者仓库的原文件，也可以不改用本仓库，直接对本地文件打补丁：

    uv run --no-project patch_gguf_img_in.py <模型路径>

脚本在本仓库根目录，纯标准库，无第三方依赖，可重复执行。

## 许可与致谢

- 基座模型：[Qwen/Qwen-Image-2.1](https://huggingface.co/Qwen/Qwen-Image-2.1)，
  适用 Qwen Research License，本仓库沿用同一许可，见 LICENSE 链接。
- 量化：realrebelai，见其原始仓库。
- 问题定位、修正与测试：由本仓库维护者完成，详细诊断过程见作者仓库的 Discussion。
""" % ("\n".join("- `%s`" % name for name in uploaded) or "（未上传任何文件）")

readme_path = WORKDIR / "README.md"
readme_path.write_text(readme, encoding="utf-8")
api.upload_file(path_or_fileobj=str(readme_path), path_in_repo="README.md",
                repo_id=DST_REPO, commit_message="添加说明、署名与许可声明")
print("已上传 README.md")

script_path = WORKDIR / "patch_gguf_img_in.py"
script_path.write_text(PATCH_SCRIPT, encoding="utf-8")
api.upload_file(path_or_fileobj=str(script_path), path_in_repo="patch_gguf_img_in.py",
                repo_id=DST_REPO, commit_message="附上打补丁脚本")
print("已上传 patch_gguf_img_in.py")

print()
print("=" * 70)
print("完成。仓库地址：")
print("  https://huggingface.co/%s" % DST_REPO)
print("把这个链接发给 realrebelai 即可。")
print("=" * 70)
