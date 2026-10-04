#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen-Image-2.1 本地启动器（文生图 / 图像编辑）

只依赖 Python 标准库，无需安装任何第三方包。

用法：
    uv run --no-project launcher.py                                  交互式
    uv run --no-project launcher.py -p "一只橘猫在溪水边喝水"            文生图
    uv run --no-project launcher.py -i outputs\\cat-water-768.png ^
                                   -p "把猫换成小狗"                    图像编辑
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SD_CLI = ROOT / "bin" / "sd-cli.exe"
OUTPUTS = ROOT / "outputs"

# 与图形界面（gui-go）用同一份扩散模型；HQv3 需先打补丁，见 README「量化版本选择」
DIFFUSION_MODEL = ROOT / "models" / "Qwen-Image-2.1-Q4_K_M-HQv3.gguf"
VAE_MODEL = ROOT / "models" / "vae" / "qwen_image_2.1_vae_bf16.safetensors"

# 优先使用 Heretic GGUF；不存在时回退官方编码器
TEXT_ENCODERS = (
    ROOT / "models" / "text_encoders" / "qwen3vl_8b_heretic-Q4_K_M.gguf",
    ROOT / "models" / "text_encoders" / "Qwen3VL-8B-Instruct-Q4_K_M.gguf",
    ROOT / "models" / "text_encoders" / "qwen3vl_8b_int8_convrot.safetensors",
)
# 视觉塔只有图像编辑才需要，文生图不加载
VISION_MODELS = (
    ROOT / "models" / "text_encoders" / "mmproj-Qwen3VL-8B-Instruct-F16.gguf",
    ROOT / "models" / "text_encoders" / "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf",
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def first_existing(candidates):
    for path in candidates:
        if path.is_file():
            return path
    return None


def die(message):
    print(f"\n[错误] {message}")
    sys.exit(1)


# ---------------------------------------------------------------- 交互输入

def ask(question, default=None):
    """单行输入，直接回车则采用默认值"""
    tip = f"（默认 {default}）" if default else ""
    while True:
        value = input(f"{question}{tip}: ").strip().strip('"')
        if value:
            return value
        if default is not None:
            return default


def ask_int(question, default, minimum=1):
    while True:
        raw = ask(question, default)
        try:
            value = int(raw)
        except ValueError:
            print("  请输入整数。")
            continue
        if value < minimum:
            print(f"  不能小于 {minimum}。")
            continue
        return value


def ask_choice(question, options, default="1"):
    print(question)
    for index, label in enumerate(options, start=1):
        print(f"  {index}) {label}")
    valid = {str(i) for i in range(1, len(options) + 1)}
    while True:
        raw = ask("  请选择", default)
        if raw in valid:
            return int(raw)
        print(f"  请输入 1-{len(options)}。")


def ask_size(default="768x768"):
    """尺寸必须能被 32 整除"""
    while True:
        raw = ask("  尺寸（宽x高）", default).lower().replace("×", "x").replace("*", "x").replace(" ", "")
        try:
            if "x" in raw:
                left, _, right = raw.partition("x")
                width, height = int(left), int(right)
            else:
                width = height = int(raw)
        except ValueError:
            print("  格式示例：768x768")
            continue
        if width % 32 or height % 32:
            print("  宽和高都必须是 32 的倍数，例如 768、1024。")
            continue
        return width, height


def ask_reference_image():
    """列出可用的参考图供选择，也支持直接粘贴路径"""
    candidates = []
    if OUTPUTS.is_dir():
        candidates = sorted(p for p in OUTPUTS.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
    if candidates:
        print("  参考图：")
        for index, path in enumerate(candidates, start=1):
            print(f"    {index}) {path.relative_to(ROOT)}")
        print("    0) 手动输入路径")
    while True:
        raw = ask("  请选择参考图", "1" if candidates else "0")
        if raw.isdigit() and candidates and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        if raw in ("0", "") and candidates:
            raw = ask("  参考图完整路径")
        path = Path(raw).expanduser()
        if path.is_file():
            return path
        print("  文件不存在，请重新输入。")


def wizard():
    """交互式向导，返回参数字典"""
    print("=" * 56)
    print("  Qwen-Image-2.1 本地生成器")
    print("=" * 56)

    mode = ask_choice(
        "第 1 步  选择模式",
        ["文生图  —— 用提示词直接画一张", "图像编辑 —— 传参考图，按提示词改图"],
        default="1",
    )
    is_edit = mode == 2

    print("\n第 2 步  输入提示词")
    if is_edit:
        print("  描述要怎么改，例如：把画面中的小猫替换成一只小狗")
    else:
        print("  描述你想要的画面，例如：一只橘猫在溪水边喝水，写实摄影风格")
    prompt = ask("  提示词")

    image = None
    if is_edit:
        print("\n第 3 步  选择参考图")
        image = ask_reference_image()
    else:
        print("\n第 3 步  参考图（文生图模式跳过）")

    print("\n第 4 步  参数（直接回车用默认值即可）")
    width, height = ask_size()
    steps = ask_int("  采样步数（越大越精细，速度越慢）", "40", minimum=1)
    seed = ask_int("  随机种子（-1 表示每次随机）", "-1", minimum=-1)
    if seed < 0:
        seed = -1

    return {
        "prompt": prompt,
        "image": image,
        "width": width,
        "height": height,
        "steps": steps,
        "seed": seed,
    }


# ---------------------------------------------------------------- 执行

def preflight():
    """运行前检查必需文件，缺什么直接说清楚"""
    missing = [path for path in (SD_CLI, DIFFUSION_MODEL, VAE_MODEL) if not path.is_file()]
    if first_existing(TEXT_ENCODERS) is None:
        missing.append(ROOT / "models" / "text_encoders")
    if missing:
        die("缺少必需文件：\n  " + "\n  ".join(str(p) for p in missing))


def build_command(params):
    text_encoder = first_existing(TEXT_ENCODERS)
    command = [
        str(SD_CLI),
        "--diffusion-model", str(DIFFUSION_MODEL),
        "--vae", str(VAE_MODEL),
        "--llm", str(text_encoder),
        # 文本编码器放 CPU：它只跑一次，显存留给扩散模型
        "--backend", "te=cpu",
        "--diffusion-fa",
        "--sampling-method", params["sampler"],
        "--cfg-scale", str(params["cfg_scale"]),
        "--steps", str(params["steps"]),
        "--width", str(params["width"]),
        "--height", str(params["height"]),
        "--seed", str(params["seed"]),
        "--output", str(params["output"]),
    ]

    if params["image"] is not None:
        vision = first_existing(VISION_MODELS)
        if vision is None:
            die("图像编辑需要视觉塔：请把 mmproj-Qwen3VL-8B-Instruct-F16.gguf 放到 models\\text_encoders\\ 下。")
        command += ["--ref-image", str(params["image"]), "--llm_vision", str(vision)]

    if params["negative_prompt"]:
        command += ["--negative-prompt", params["negative_prompt"]]

    # 提示词放最后，避免以 - 开头的文本被当成选项
    command += ["--prompt", params["prompt"]]
    return command


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Qwen-Image-2.1 本地启动器")
    parser.add_argument("-p", "--prompt", help="提示词；提供后不再进入交互向导")
    parser.add_argument("-P", "--prompt-file", dest="prompt_file",
                        help="从 UTF-8 文本文件读提示词，优先于 -p（适合长提示词，避免命令行编码问题）")
    parser.add_argument("-i", "--image", help="参考图路径，提供即为图像编辑模式")
    parser.add_argument("-n", "--negative-prompt", default="", help="负面提示词")
    parser.add_argument("-W", "--width", type=int, default=768)
    parser.add_argument("-H", "--height", type=int, default=768)
    parser.add_argument("-s", "--steps", type=int, default=40)
    parser.add_argument("--cfg-scale", type=float, default=1.0)
    parser.add_argument("--sampler", default="euler")
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("-o", "--output", help="输出路径，默认 outputs\\qwen-<尺寸>-<时间>.png")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不实际运行")
    args = parser.parse_args()

    preflight()

    prompt_text = args.prompt
    if args.prompt_file:
        prompt_path = Path(args.prompt_file).expanduser()
        if not prompt_path.is_absolute():
            prompt_path = ROOT / prompt_path
        if not prompt_path.is_file():
            die(f"提示词文件不存在：{prompt_path}")
        prompt_text = prompt_path.read_text(encoding="utf-8").strip()

    if prompt_text is None:
        params = wizard()
        params["sampler"] = args.sampler
        params["cfg_scale"] = args.cfg_scale
        params["negative_prompt"] = args.negative_prompt
        params["output"] = args.output
    else:
        params = {
            "prompt": prompt_text,
            "image": Path(args.image).expanduser() if args.image else None,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "seed": args.seed if args.seed >= 0 else -1,
            "sampler": args.sampler,
            "cfg_scale": args.cfg_scale,
            "negative_prompt": args.negative_prompt,
            "output": args.output,
        }

    if params["width"] % 32 or params["height"] % 32:
        die("宽和高必须是 32 的倍数。")
    if params["image"] is not None:
        if not params["image"].is_file():
            die(f"参考图不存在：{params['image']}")
        params["image"] = params["image"].resolve()

    if params["output"]:
        output = Path(params["output"]).expanduser()
        if not output.is_absolute():
            output = ROOT / output
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = OUTPUTS / f"qwen-{params['width']}x{params['height']}-{stamp}.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    params["output"] = output

    command = build_command(params)

    print("\n" + "-" * 56)
    print(f"模式   : {'图像编辑' if params['image'] else '文生图'}")
    print(f"提示词 : {params['prompt']}")
    if params["image"]:
        print(f"参考图 : {params['image']}")
    print(f"尺寸   : {params['width']}x{params['height']}  步数 {params['steps']}  种子 {params['seed']}")
    print(f"输出   : {output}")
    print("-" * 56)

    if args.dry_run:
        print("\n[dry-run] " + " ".join(command))
        return

    print("\n开始生成。首次运行需从磁盘加载模型，约 10 秒；期间请勿关闭窗口。\n")

    started = time.time()
    try:
        result = subprocess.run(command, cwd=ROOT)
    except FileNotFoundError:
        die(f"找不到可执行文件：{SD_CLI}")
    except KeyboardInterrupt:
        print("\n\n已中断。")
        sys.exit(130)

    if result.returncode != 0:
        die(f"sd-cli 退出码 {result.returncode}，生成失败，请查看上方日志。")
    if not output.is_file():
        die("命令已结束但没有产生输出文件，请查看上方日志。")

    elapsed = time.time() - started
    size_mb = output.stat().st_size / 1024 / 1024
    print(f"\n完成：{output}")
    print(f"耗时 {elapsed:.1f} 秒，文件 {size_mb:.2f} MB")


if __name__ == "__main__":
    main()
