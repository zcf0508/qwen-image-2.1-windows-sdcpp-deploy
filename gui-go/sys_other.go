//go:build !windows

package main

import "os/exec"

func hideWindow(cmd *exec.Cmd) {}

func bindChildLifetime(cmd *exec.Cmd) uintptr { return 0 }

// 资源读数只在 Windows 上实现；其他平台显示占位符。
func resourceText() (vram, ram string) {
	return "显存 —", "内存 —"
}
