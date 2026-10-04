package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

const (
	serverPort = 1234
	baseURL    = "http://127.0.0.1:1234"
)

var (
	rootDir = findRoot()

	sdServerPath   = filepath.Join(rootDir, "bin", "sd-server.exe")
	outputsDir     = filepath.Join(rootDir, "outputs")
	diffusionModel = filepath.Join(rootDir, "models", "Qwen-Image-2.1-Q4_K_M-HQv3.gguf")
	vaeModel       = filepath.Join(rootDir, "models", "vae", "qwen_image_2.1_vae_bf16.safetensors")

	// 优先使用 Heretic GGUF；不存在时回退官方编码器
	textEncoders = []string{
		filepath.Join(rootDir, "models", "text_encoders", "qwen3vl_8b_heretic-Q4_K_M.gguf"),
		filepath.Join(rootDir, "models", "text_encoders", "Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
		filepath.Join(rootDir, "models", "text_encoders", "qwen3vl_8b_int8_convrot.safetensors"),
	}
	// 视觉塔只在图像编辑时需要，加载它要多占约 1.1 GB 内存
	visionModels = []string{
		filepath.Join(rootDir, "models", "text_encoders", "mmproj-Qwen3VL-8B-Instruct-F16.gguf"),
		filepath.Join(rootDir, "models", "text_encoders", "mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"),
	}
)

// findRoot 从可执行文件所在目录向上找包含 bin/sd-server.exe 的项目根目录，
// 兼容开发构建（二进制在 .mygo 或 build 子目录里）与免安装部署两种布局。
func findRoot() string {
	exe, err := os.Executable()
	if err != nil {
		exe = "."
	}
	dir := filepath.Dir(exe)
	for range 5 {
		if isFile(filepath.Join(dir, "bin", "sd-server.exe")) {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	wd, err := os.Getwd()
	if err != nil {
		return filepath.Dir(exe)
	}
	return wd
}

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func firstExisting(candidates []string) string {
	for _, path := range candidates {
		if isFile(path) {
			return path
		}
	}
	return ""
}

// ---------------------------------------------------------------- HTTP

// httpError 保留服务端返回的状态码与正文，取消任务的 409 要靠它区分。
type httpError struct {
	status int
	body   string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d：%s", e.status, e.body)
}

func httpJSON(ctx context.Context, url string, payload any, timeout time.Duration) (map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	var body io.Reader
	method := http.MethodGet
	if payload != nil {
		data, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		body = bytes.NewReader(data)
		method = http.MethodPost
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return nil, err
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		text := string(data)
		if len(text) > 400 {
			text = text[:400]
		}
		return nil, &httpError{status: resp.StatusCode, body: text}
	}
	if len(data) == 0 {
		return map[string]any{}, nil
	}
	var result map[string]any
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func serverAlive() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := httpJSON(ctx, baseURL+"/sdcpp/v1/capabilities", nil, 2*time.Second)
	return err == nil
}

// ---------------------------------------------------------------- 服务进程

// QwenServer 管理 sd-server.exe 的启动、停止与任务提交。
type QwenServer struct {
	logf func(string)

	mu         sync.Mutex
	cmd        *exec.Cmd
	done       chan struct{} // 进程退出后关闭；nil 表示没有进程
	withVision bool
	jobID      string
	adopted    bool
	jobObject  uintptr // Windows Job 对象句柄，见 bindChildLifetime
}

func (s *QwenServer) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.adopted {
		return true
	}
	return s.cmd != nil && !s.exitedLocked()
}

// Stoppable 报告服务是否由本窗口启动、可以在此停止。
func (s *QwenServer) Stoppable() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cmd != nil && !s.exitedLocked()
}

func (s *QwenServer) Adopted() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.adopted
}

func (s *QwenServer) WithVision() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.withVision
}

func (s *QwenServer) JobID() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.jobID
}

func (s *QwenServer) SetJobID(id string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.jobID = id
}

func (s *QwenServer) exitedLocked() bool {
	select {
	case <-s.done:
		return true
	default:
		return false
	}
}

// Start 启动服务并等待模型就绪。返回 (成功, 消息)。
func (s *QwenServer) Start(withVision bool, timeout time.Duration) (bool, string) {
	if serverAlive() {
		s.mu.Lock()
		s.adopted = true
		s.withVision = withVision
		s.mu.Unlock()
		s.logf("检测到 127.0.0.1:1234 已有服务，直接复用；该服务非本窗口启动，无法在此停止。")
		return true, "已复用运行中的服务"
	}

	encoder := firstExisting(textEncoders)
	missing := []string{}
	for _, path := range []string{sdServerPath, diffusionModel, vaeModel, encoder} {
		if path == "" || !isFile(path) {
			missing = append(missing, path)
		}
	}
	if len(missing) > 0 {
		return false, "缺少必需文件：\n" + joinLines(missing)
	}

	args := []string{
		"--diffusion-model", diffusionModel,
		"--vae", vaeModel,
		"--llm", encoder,
		// 文本编码器放 CPU，显存留给扩散模型
		"--backend", "te=cpu",
		"--diffusion-fa",
		"--listen-ip", "127.0.0.1",
		"--listen-port", fmt.Sprint(serverPort),
	}
	if withVision {
		vision := firstExisting(visionModels)
		if vision == "" {
			return false, "图像编辑需要视觉塔：请把 mmproj-Qwen3VL-8B-Instruct-F16.gguf 放到 models\\text_encoders\\ 下。"
		}
		args = append(args, "--llm_vision", vision)
	}

	if withVision {
		s.logf("启动服务：已加载视觉塔")
	} else {
		s.logf("启动服务：未加载视觉塔")
	}
	cmd := exec.Command(sdServerPath, args...)
	cmd.Dir = rootDir
	hideWindow(cmd)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return false, err.Error()
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return false, err.Error()
	}

	done := make(chan struct{})
	s.mu.Lock()
	s.cmd = cmd
	s.done = done
	s.withVision = withVision
	s.adopted = false
	s.mu.Unlock()

	// 绑定生命周期：本进程结束（含崩溃或被强杀）时子进程一并退出，避免残留占用显存
	s.jobObject = bindChildLifetime(cmd)

	// 把服务端日志转发到界面
	go func() {
		buf := make([]byte, 0, 4096)
		chunk := make([]byte, 4096)
		flush := func() {
			if len(buf) > 0 {
				s.logf(string(buf))
				buf = buf[:0]
			}
		}
		for {
			n, err := stdout.Read(chunk)
			for _, b := range chunk[:n] {
				if b == '\n' {
					flush()
				} else if b != '\r' {
					buf = append(buf, b)
				}
			}
			if err != nil {
				flush()
				break
			}
		}
		cmd.Wait()
		close(done)
	}()

	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		exited := s.cmd == nil || s.exitedLocked()
		s.mu.Unlock()
		if exited {
			return false, "服务进程意外退出，请查看下方日志。"
		}
		if serverAlive() {
			s.logf("服务已就绪；模型在首次出图时载入，之后常驻。")
			return true, "服务已就绪"
		}
		time.Sleep(800 * time.Millisecond)
	}
	return false, "等待服务就绪超时。"
}

// Stop 停止服务并等待进程真正退出。进程退出即释放显存与内存。
func (s *QwenServer) Stop() {
	s.SetJobID("")
	s.mu.Lock()
	if s.adopted {
		s.mu.Unlock()
		s.logf("该服务由其他窗口启动，本窗口无法停止。")
		return
	}
	cmd, done := s.cmd, s.done
	s.cmd, s.done = nil, nil
	s.mu.Unlock()

	if cmd == nil || cmd.Process == nil {
		return
	}
	select {
	case <-done:
		return
	default:
	}
	// Windows 上没有温和的 terminate，直接结束进程；sd-server 无持久状态需要保存
	_ = cmd.Process.Kill()
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		s.logf("警告：服务进程未能在超时内退出，可能仍在占用资源。")
		return
	}
	s.logf("服务已停止，显存与内存已释放。")
}

// Submit 提交生成任务，返回任务 ID。
func (s *QwenServer) Submit(payload map[string]any) (string, error) {
	result, err := httpJSON(context.Background(), baseURL+"/sdcpp/v1/img_gen", payload, 30*time.Second)
	if err != nil {
		return "", err
	}
	id, _ := result["id"].(string)
	s.SetJobID(id)
	return id, nil
}

func (s *QwenServer) Job(jobID string) (map[string]any, error) {
	return httpJSON(context.Background(), baseURL+"/sdcpp/v1/jobs/"+jobID, nil, 15*time.Second)
}

// Cancel 请求取消任务；服务端不支持中断生成中的任务时返回 409 的 *httpError。
func (s *QwenServer) Cancel(jobID string) error {
	_, err := httpJSON(context.Background(), baseURL+"/sdcpp/v1/jobs/"+jobID+"/cancel", map[string]any{}, 15*time.Second)
	return err
}

func joinLines(lines []string) string {
	out := ""
	for i, line := range lines {
		if i > 0 {
			out += "\n"
		}
		out += line
	}
	return out
}
