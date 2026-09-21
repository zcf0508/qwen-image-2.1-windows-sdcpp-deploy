"""修正 ComfyUI-GGUF 导出模型里 img_in.weight 的形状声明。

问题
----
realrebelai 的 HQv3 GGUF 由 ComfyUI-GGUF 转换器导出，它把 img_in.weight 从
[64, 4096] 重排为 [256, 1024]（输出维除以 4、输入维乘以 4），并把原始形状记在
元数据 comfy.gguf.orig_shape.img_in.weight 里。

stable-diffusion.cpp 恰好用这个张量的形状推断网络规模
（src/model/diffusion/qwen_image_2_1.hpp）：

    config.in_channels = w->ne[0];
    config.hidden_size = w->ne[1];

被重排后它会按 hidden = 1024 去建一个 7B 网络，与文件里 4096 维的张量全线不符，
报 "model metadata validation failed" 并拒绝加载。

做法
----
把该张量在张量表里的两个 uint64 改回 [64, 4096]。元素总数 262144 改前改后一致，
所以只是标签改写，张量数据一格都不用移动，文件大小也不变（实测只有 3 个字节变化）。

用法
----
    uv run --no-project patch_gguf_img_in.py
    uv run --no-project patch_gguf_img_in.py <模型路径> [输出维 输入维]

改前会把文件头备份为 <模型>.header.bak，要还原就直接覆盖回去。
脚本可重复执行：形状已经是目标值时不做任何改动。
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = ROOT / "models" / "Qwen-Image-2.1-Q4_K_M-HQv3.gguf"
TARGET_TENSOR = "img_in.weight"
DEFAULT_SHAPE = (64, 4096)
HEADER_BACKUP_BYTES = 1 << 20

_SCALAR_WIDTH = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _read_exact(handle, count: int) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise EOFError("文件在预期位置提前结束，可能不是完整的 GGUF")
    return data


def _skip_metadata_value(handle, value_type: int) -> None:
    if value_type in _SCALAR_WIDTH:
        _read_exact(handle, _SCALAR_WIDTH[value_type])
    elif value_type == 8:  # 字符串
        _read_exact(handle, struct.unpack("<Q", _read_exact(handle, 8))[0])
    elif value_type == 9:  # 数组
        element_type = struct.unpack("<I", _read_exact(handle, 4))[0]
        count = struct.unpack("<Q", _read_exact(handle, 8))[0]
        if element_type == 8:
            for _ in range(count):
                _read_exact(handle, struct.unpack("<Q", _read_exact(handle, 8))[0])
        elif element_type in _SCALAR_WIDTH:
            _read_exact(handle, _SCALAR_WIDTH[element_type] * count)
        else:
            raise ValueError("数组元素类型 %d 无法处理" % element_type)
    else:
        raise ValueError("元数据取值类型 %d 无法处理" % value_type)


def locate_shape_field(handle, tensor_name: str):
    """定位张量形状字段，返回 (文件偏移, 维度数, 当前形状)"""
    handle.seek(0)
    if _read_exact(handle, 4) != b"GGUF":
        raise ValueError("前四字节不是 GGUF 魔数")

    version = struct.unpack("<I", _read_exact(handle, 4))[0]
    tensor_count = struct.unpack("<Q", _read_exact(handle, 8))[0]
    kv_count = struct.unpack("<Q", _read_exact(handle, 8))[0]

    for _ in range(kv_count):
        _read_exact(handle, struct.unpack("<Q", _read_exact(handle, 8))[0])  # 键名
        _skip_metadata_value(handle, struct.unpack("<I", _read_exact(handle, 4))[0])

    for _ in range(tensor_count):
        name_length = struct.unpack("<Q", _read_exact(handle, 8))[0]
        name = _read_exact(handle, name_length).decode("utf-8", "replace")
        n_dims = struct.unpack("<I", _read_exact(handle, 4))[0]
        field_offset = handle.tell()
        shape = [struct.unpack("<Q", _read_exact(handle, 8))[0] for _ in range(n_dims)]
        _read_exact(handle, 4 + 8)  # 量化类型编号 + 数据偏移
        if name == tensor_name:
            return version, field_offset, n_dims, shape

    raise LookupError("张量表里没有 %s" % tensor_name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="修正 GGUF 中 img_in.weight 的形状声明")
    parser.add_argument("model", nargs="?", default=str(DEFAULT_MODEL), help="GGUF 文件路径")
    parser.add_argument("shape", nargs="*", type=int, default=list(DEFAULT_SHAPE),
                        help="目标形状，默认 64 4096（ggml 记法：输入维在前）")
    args = parser.parse_args(argv)

    path = Path(args.model)
    if not path.is_file():
        print("找不到模型文件：%s" % path)
        return 1
    target = list(args.shape)

    with open(path, "rb") as handle:
        version, offset, n_dims, current = locate_shape_field(handle, TARGET_TENSOR)

    print("模型      %s" % path)
    print("GGUF 版本 %d" % version)
    print("%s 形状字段：偏移 %d，%d 个 uint64" % (TARGET_TENSOR, offset, n_dims))
    print("当前形状  %s" % current)
    print("目标形状  %s" % target)

    if current == target:
        print("\n形状已经是目标值，无需改动。")
        return 0
    if len(target) != n_dims:
        print("\n维度数量不一致，拒绝改动。")
        return 2

    current_elements = 1
    for dim in current:
        current_elements *= dim
    target_elements = 1
    for dim in target:
        target_elements *= dim
    if current_elements != target_elements:
        print("\n元素总数不同（%d 对 %d），这说明数据布局真的变了，拒绝改动。"
              % (current_elements, target_elements))
        return 3
    print("元素总数  %d，改前改后一致，张量数据无需移动" % current_elements)

    backup = path.with_name(path.name + ".header.bak")
    with open(path, "rb") as src, open(backup, "wb") as dst:
        dst.write(src.read(HEADER_BACKUP_BYTES))
    print("\n文件头已备份到 %s" % backup.name)

    with open(path, "r+b") as handle:
        handle.seek(offset)
        before = handle.read(n_dims * 8)
        actual = [struct.unpack("<Q", before[i * 8:i * 8 + 8])[0] for i in range(n_dims)]
        if actual != current:
            print("写入前回读的形状与首次解析不一致，中止。")
            return 4
        handle.seek(offset)
        for value in target:
            handle.write(struct.pack("<Q", value))
        handle.flush()

    with open(path, "rb") as handle:
        _, _, _, now = locate_shape_field(handle, TARGET_TENSOR)
    print("改后回读  %s" % now)
    if now != target:
        print("校验失败")
        return 5
    print("校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
