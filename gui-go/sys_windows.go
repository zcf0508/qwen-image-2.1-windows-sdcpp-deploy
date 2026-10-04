//go:build windows

package main

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const createNoWindow = 0x08000000

// hideWindow 让子进程不弹控制台窗口。
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow, HideWindow: true}
}

// bindChildLifetime 把子进程绑进 Job 对象：本进程一旦结束（含崩溃或被强杀），
// 子进程随之终止。否则 GUI 被强制结束时会残留 sd-server.exe，显存与内存都不释放。
// 失败时返回 0，不影响正常流程。
func bindChildLifetime(cmd *exec.Cmd) uintptr {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	_, err = windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	)
	if err != nil {
		windows.CloseHandle(job)
		return 0
	}
	processHandle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		windows.CloseHandle(job)
		return 0
	}
	defer windows.CloseHandle(processHandle)
	if err := windows.AssignProcessToJobObject(job, processHandle); err != nil {
		windows.CloseHandle(job)
		return 0
	}
	return uintptr(job)
}

// memoryStatusEx 与 GlobalMemoryStatusEx 对应；x/sys 未包装该函数，直接调 kernel32。
type memoryStatusEx struct {
	dwLength                uint32
	dwMemoryLoad            uint32
	ullTotalPhys            uint64
	ullAvailPhys            uint64
	ullTotalPageFile        uint64
	ullAvailPageFile        uint64
	ullTotalVirtual         uint64
	ullAvailVirtual         uint64
	ullAvailExtendedVirtual uint64
}

var procGlobalMemoryStatusEx = windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")

// ramUsage 返回 (已用GB, 总量GB, 成功)。
func ramUsage() (float64, float64, bool) {
	status := memoryStatusEx{dwLength: uint32(unsafe.Sizeof(memoryStatusEx{}))}
	ret, _, _ := procGlobalMemoryStatusEx.Call(uintptr(unsafe.Pointer(&status)))
	if ret == 0 {
		return 0, 0, false
	}
	const gb = 1024.0 * 1024.0 * 1024.0
	total := float64(status.ullTotalPhys) / gb
	used := float64(status.ullTotalPhys-status.ullAvailPhys) / gb
	return used, total, true
}

// vramUsage 返回 (已用MB, 总量MB, 成功)；没有 nvidia-smi 时 ok 为 false。
func vramUsage() (int, int, bool) {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "nvidia-smi",
		"--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits")
	hideWindow(cmd)
	out, err := cmd.Output()
	if err != nil {
		return 0, 0, false
	}
	line, _, _ := strings.Cut(strings.TrimSpace(string(out)), "\n")
	parts := strings.Split(strings.TrimSpace(line), ",")
	if len(parts) != 2 {
		return 0, 0, false
	}
	used, err1 := strconv.Atoi(strings.TrimSpace(parts[0]))
	total, err2 := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err1 != nil || err2 != nil {
		return 0, 0, false
	}
	return used, total, true
}

func resourceText() (vram, ram string) {
	if used, total, ok := vramUsage(); ok {
		vram = fmt.Sprintf("显存 %d / %d MB", used, total)
	} else {
		vram = "显存 —"
	}
	if used, total, ok := ramUsage(); ok {
		ram = fmt.Sprintf("内存 %.1f / %.1f GB", used, total)
	} else {
		ram = "内存 —"
	}
	return vram, ram
}
