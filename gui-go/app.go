package main

import (
	"encoding/base64"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
)

var imageSuffixes = map[string]bool{".png": true, ".jpg": true, ".jpeg": true, ".webp": true, ".bmp": true}

// app 是窗口要显示的全部状态。界面是它的函数，在主线程上逐帧构建；
// 后台协程只能通过 update 改它。
type app struct {
	win *mygo.Window

	server          *QwenServer
	serviceStarting bool
	stateText       string
	stateKind       string // idle | busy | ready | error

	vramText, ramText string

	mode        string // txt2img | edit
	refPaths    []string
	refSelected int
	refThumbs   map[string]*ui.Bitmap // 参考图缩略图，加入时解码一次
	prompt      string
	negative    string

	hd               bool
	width, height    string
	steps, cfg, seed string
	sizeText         string
	paramError       string
	paramsValid      bool

	splitPos float32

	busy, cancelPending, forceStopping bool
	progressText                       string
	progressActive                     bool

	resultPath string
	preview    *ui.Bitmap

	logs       []string
	logVisible bool
	logScroll  ui.ScrollState

	closeConfirmed  bool
	adoptedNotified bool
	editWarnLogged  bool
	genStarted      time.Time
	genEstimate     float64 // 本次生成的预计秒数，画进度条用
	genAspect       float32 // 本次生成的宽高比，画占位画框用
	done            chan struct{}
}

func newApp() *app {
	a := &app{
		mode:        "txt2img",
		refSelected: -1,
		refThumbs:   map[string]*ui.Bitmap{},
		width:       "1024",
		height:      "1024",
		steps:       "40",
		cfg:         "1.0",
		seed:        "-1",
		splitPos:    372,
		stateKind:   "idle",
		vramText:    "显存 —",
		ramText:     "内存 —",
		done:        make(chan struct{}),
	}
	a.stateText = stateIdleText
	a.server = &QwenServer{logf: a.logf}
	a.refreshParams()
	a.appendLog("就绪。填写画面描述后按 Ctrl+Enter；服务未启动时会自动加载。")
	return a
}

// update 在主线程上执行 fn 并重绘一帧；测试里没有窗口，直接执行。
func (a *app) update(fn func()) {
	if a.win != nil {
		a.win.Update(fn)
	} else {
		fn()
	}
}

// logf 可从任意协程调用。
func (a *app) logf(text string) {
	a.update(func() { a.appendLog(text) })
}

func (a *app) appendLog(text string) {
	a.logs = append(a.logs, text)
	if len(a.logs) > 600 {
		a.logs = slices.Delete(a.logs, 0, 100)
	}
	// 日志停在底部才跟着滚动，用户往上翻就不打扰
	if a.logScroll.Y >= a.logScroll.MaxY {
		a.logScroll.Y = float32(math.MaxFloat32)
	}
}

func (a *app) setState(text, kind string) {
	a.stateText, a.stateKind = text, kind
}

// ------------------------------------------------------------ 参数与预设

func (a *app) activePresets() []sizePreset {
	if a.hd {
		return sizePresetsHD
	}
	return sizePresets
}

func (a *app) refreshParams() {
	_, err := parseGenerationParameters(a.width, a.height, a.steps, a.cfg, a.seed)
	a.paramsValid = err == nil
	if err != nil {
		a.paramError = err.Error()
	} else {
		a.paramError = ""
	}
	w, werr := parseInt(a.width)
	h, herr := parseInt(a.height)
	if werr != nil || herr != nil {
		a.sizeText = "宽高需要是整数"
		return
	}
	mp := float64(w) * float64(h) / 1e6
	a.sizeText = fmt.Sprintf("当前 %d × %d（约 %.2f MP，预计 %.0f 秒）", w, h, mp, mp*secondsPerMP)
}

func parseInt(text string) (int, error) {
	var n int
	_, err := fmt.Sscanf(strings.TrimSpace(text), "%d", &n)
	if err == nil && fmt.Sprint(n) != strings.TrimSpace(text) {
		return 0, fmt.Errorf("not an integer")
	}
	return n, err
}

func (a *app) choosePreset(p sizePreset) {
	a.width = fmt.Sprint(p.width)
	a.height = fmt.Sprint(p.height)
	a.refreshParams()
}

// setHD 切换标准/高清档，并把当前选中的比例换成新档里同比例的尺寸；
// 自定义尺寸保持不变。
func (a *app) setHD(on bool) {
	label := ""
	if i := a.selectedPreset(); i >= 0 {
		label = a.activePresets()[i].label
	}
	a.hd = on
	for _, p := range a.activePresets() {
		if p.label == label {
			a.choosePreset(p)
			return
		}
	}
	a.refreshParams()
}

func (a *app) selectedPreset() int {
	w, werr := parseInt(a.width)
	h, herr := parseInt(a.height)
	if werr != nil || herr != nil {
		return -1
	}
	for i, p := range a.activePresets() {
		if p.width == w && p.height == h {
			return i
		}
	}
	return -1
}

// ------------------------------------------------------------ 模式与参考图

func (a *app) modeHint() (string, bool) {
	if a.mode != "edit" {
		return "直接描述想要的画面；无需参考图。", false
	}
	if a.server.Running() && !a.server.WithVision() {
		return "当前服务未加载编辑能力，生成时会自动重启并加载。", true
	}
	return "添加参考图并描述修改（图片也可直接拖进窗口）；编辑能力会自动加载。", false
}

func (a *app) addRefs(paths []string) int {
	added := 0
	for _, raw := range paths {
		if slices.Contains(a.refPaths, raw) {
			continue
		}
		a.refPaths = append(a.refPaths, raw)
		a.refThumbs[raw] = loadThumb(raw)
		added++
	}
	if added > 0 {
		a.mode = "edit"
	}
	return added
}

func (a *app) clearRefs() {
	a.refPaths = nil
	a.refSelected = -1
	clear(a.refThumbs)
}

// loadThumb 解码参考图做缩略图；读不了的图返回 nil，界面显示文件名占位。
func loadThumb(path string) *ui.Bitmap {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	bitmap, err := ui.DecodeBitmap(data)
	if err != nil {
		return nil
	}
	return bitmap
}

func (a *app) filesDropped(paths []string) {
	images := []string{}
	others := []string{}
	for _, path := range paths {
		if isFile(path) && imageSuffixes[strings.ToLower(filepath.Ext(path))] {
			images = append(images, path)
		} else {
			others = append(others, filepath.Base(path))
		}
	}
	if len(others) > 0 {
		a.appendLog("已忽略非图片内容：" + strings.Join(others, "、"))
	}
	if len(images) == 0 {
		return
	}
	if a.addRefs(images) > 0 {
		a.appendLog(fmt.Sprintf("拖入 %d 张图片，参考图共 %d 张。", len(images), len(a.refPaths)))
	} else {
		a.appendLog("拖入的图片已在参考图里，未重复添加。")
	}
}

func (a *app) pickRefs() {
	go func() {
		initial := outputsDir
		if !isDir(initial) {
			initial = rootDir
		}
		paths, err := mygo.Dialog.Open(mygo.OpenDialogOptions{
			Parent:   a.win,
			Title:    "选择参考图（可多选）",
			Filters:  []mygo.FileFilter{{Name: "图片", Extensions: []string{"png", "jpg", "jpeg", "webp", "bmp"}}},
			Multiple: true,
		})
		if err != nil || len(paths) == 0 {
			return
		}
		a.update(func() {
			if a.addRefs(paths) > 0 {
				a.refSelected = len(a.refPaths) - 1
			}
		})
	}()
}

func isDir(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

func (a *app) moveRef(delta int) {
	i := a.refSelected
	target := i + delta
	if i < 0 || target < 0 || target >= len(a.refPaths) {
		a.appendLog("先在列表里点一张参考图，再调顺序。")
		return
	}
	a.refPaths[i], a.refPaths[target] = a.refPaths[target], a.refPaths[i]
	a.refSelected = target
}

func (a *app) removeRef() {
	i := a.refSelected
	if i < 0 || i >= len(a.refPaths) {
		a.appendLog("先在列表里点一张参考图，再移除。")
		return
	}
	delete(a.refThumbs, a.refPaths[i])
	a.refPaths = slices.Delete(a.refPaths, i, i+1)
	switch {
	case i < len(a.refPaths):
		a.refSelected = i
	case len(a.refPaths) > 0:
		a.refSelected = len(a.refPaths) - 1
	default:
		a.refSelected = -1
	}
}

// ------------------------------------------------------------ 服务控制

func (a *app) startService() {
	if a.server.Running() || a.serviceStarting {
		return
	}
	a.serviceStarting = true
	a.setState("正在加载模型…", "busy")
	withVision := a.mode == "edit"
	go func() {
		ok, message := a.server.Start(withVision, 300*time.Second)
		a.logf(message)
		a.update(func() {
			a.serviceStarting = false
			if ok {
				a.setState(stateReadyText, "ready")
			} else {
				a.setState(stateIdleText, "error")
				a.logVisible = true
				a.showError("启动失败", message)
			}
		})
	}()
}

func (a *app) stopService(force bool) {
	if a.busy && !force {
		a.showError("提示", "正在生成，请先取消任务；若无法取消可强制停止服务。")
		return
	}
	if force {
		a.forceStopping = true
		a.progressText = "正在强制停止服务…"
	}
	go func() {
		a.server.Stop()
		a.update(func() {
			a.setState(stateIdleText, "idle")
		})
	}()
}

func (a *app) cancelJob() {
	jobID := a.server.JobID()
	if jobID == "" {
		a.appendLog("任务仍在准备中，暂时没有可取消的任务 ID。")
		return
	}
	a.cancelPending = true
	go func() {
		err := a.server.Cancel(jobID)
		a.update(func() {
			a.cancelPending = false
			if err == nil {
				a.appendLog("已请求取消任务。")
				return
			}
			var httpErr *httpError
			if errors.As(err, &httpErr) && httpErr.status == 409 {
				a.appendLog("服务端不支持中断正在生成的画面（cancel_generating=false）。")
				a.confirmForceStop()
			} else {
				a.appendLog("取消失败：" + err.Error())
			}
		})
	}()
}

func (a *app) confirmForceStop() {
	if !a.busy {
		return
	}
	go func() {
		res, err := mygo.Dialog.Message(mygo.MessageOptions{
			Parent:  a.win,
			Type:    mygo.MessageWarning,
			Message: "当前版本无法中断正在进行的生成。\n是否强制停止服务？这会中断本次生成并立即释放显存与内存。",
			Buttons: []string{"强制停止", "继续等待"},
		})
		if err == nil && res.Button == 0 {
			a.update(func() { a.stopService(true) })
		}
	}()
}

// ------------------------------------------------------------ 生成

func (a *app) generate() {
	if a.busy || a.serviceStarting {
		return
	}
	prompt := strings.TrimSpace(a.prompt)
	if prompt == "" {
		a.showError("缺少画面描述", "请先描述想要生成的画面。")
		return
	}
	editing := a.mode == "edit"
	if editing && len(a.refPaths) == 0 {
		a.showError("缺少参考图", "图像编辑模式需要至少一张参考图。")
		return
	}
	params, err := parseGenerationParameters(a.width, a.height, a.steps, a.cfg, a.seed)
	if err != nil {
		a.showError("参数有误", err.Error())
		return
	}

	req := genRequest{
		prompt:   prompt,
		negative: strings.TrimSpace(a.negative),
		editing:  editing,
		refPaths: slices.Clone(a.refPaths),
		params:   params,
	}

	a.busy = true
	a.server.SetJobID("")
	estimate := req.estimateSeconds()
	if editing {
		a.progressText = fmt.Sprintf("准备中 · 基础生成约 %.0f 秒，图像编辑会更久", estimate)
	} else {
		a.progressText = fmt.Sprintf("准备中 · 预计约 %.0f 秒", estimate)
	}
	a.setState("生成中…", "busy")
	a.progressActive = true
	a.genStarted = time.Now()
	a.genEstimate = estimate
	a.genAspect = float32(params.width) / float32(params.height)

	go a.generateWorker(req)
}

func (a *app) generateWorker(req genRequest) {
	fail := func(msg string) { a.update(func() { a.onFail(msg) }) }

	if !serverAlive() {
		a.logf("服务未运行，正在启动…")
		ok, message := a.server.Start(req.editing, 300*time.Second)
		if !ok {
			fail(message)
			return
		}
	}
	if req.editing && !a.server.WithVision() {
		a.logf("当前服务未加载视觉塔，正在重启服务…")
		a.server.Stop()
		ok, message := a.server.Start(true, 300*time.Second)
		if !ok {
			fail(message)
			return
		}
	}

	p := req.params
	a.logf(fmt.Sprintf("提交任务：%dx%d（%.2f MP），步数 %d 引导 %g",
		p.width, p.height, float64(p.width)*float64(p.height)/1e6, p.steps, p.cfg))
	payload, err := req.buildPayload()
	if err != nil {
		fail(err.Error())
		return
	}
	if req.editing {
		names := make([]string, len(req.refPaths))
		for i, path := range req.refPaths {
			names[i] = fmt.Sprintf("图%d %s", i+1, filepath.Base(path))
		}
		a.logf("参考图顺序：" + strings.Join(names, "，"))
	}

	jobID, err := a.server.Submit(payload)
	if err != nil {
		fail("提交失败：" + err.Error())
		return
	}
	if jobID == "" {
		fail("服务未返回任务 ID。")
		return
	}

	started := time.Now()
	var info map[string]any
	var status string
	for {
		time.Sleep(time.Second)
		info, err = a.server.Job(jobID)
		if err != nil {
			fail("查询任务失败：" + err.Error())
			return
		}
		status, _ = info["status"].(string)
		if status == "queued" || status == "generating" {
			word := "生成中…"
			if status == "queued" {
				word = "排队中…"
			}
			elapsed := time.Since(started).Seconds()
			a.update(func() {
				a.progressText = fmt.Sprintf("%s 已用 %.0f 秒", word, elapsed)
			})
			continue
		}
		break
	}
	elapsed := time.Since(started).Seconds()

	switch status {
	case "cancelled":
		a.update(func() { a.onCancelled("任务已取消。") })
	case "completed":
		result, _ := info["result"].(map[string]any)
		images, _ := result["images"].([]any)
		if len(images) == 0 {
			fail("服务未返回图像。")
			return
		}
		b64, _ := images[0].(map[string]any)["b64_json"].(string)
		data, err := base64.StdEncoding.DecodeString(b64)
		if err != nil {
			fail("解码图像失败：" + err.Error())
			return
		}
		prefix := "qwen"
		if req.editing {
			prefix = "edit"
		}
		if err := os.MkdirAll(outputsDir, 0o755); err != nil {
			fail("创建输出目录失败：" + err.Error())
			return
		}
		name := fmt.Sprintf("%s-%dx%d-%s.png", prefix, p.width, p.height, time.Now().Format("20060102-150405"))
		path := filepath.Join(outputsDir, name)
		if err := os.WriteFile(path, data, 0o644); err != nil {
			fail("写入图像失败：" + err.Error())
			return
		}
		a.update(func() { a.onDone(path, elapsed) })
	default:
		message := "未知错误"
		if e, ok := info["error"].(map[string]any); ok {
			if m, ok := e["message"].(string); ok && m != "" {
				message = m
			}
		}
		fail("生成失败：" + message)
	}
}

func (a *app) onCancelled(message string) {
	a.busy = false
	a.cancelPending = false
	a.server.SetJobID("")
	a.progressActive = false
	a.progressText = message
	a.setState(stateReadyText, "ready")
	a.appendLog(message)
}

func (a *app) onDone(path string, elapsed float64) {
	a.busy = false
	a.cancelPending = false
	a.server.SetJobID("")
	a.progressActive = false

	a.resultPath = path
	a.showImage(path)
	a.progressText = fmt.Sprintf("%s · 耗时 %.1f 秒", filepath.Base(path), elapsed)
	a.setState(stateReadyText, "ready")
	a.appendLog("完成：" + path)
}

func (a *app) onFail(message string) {
	intentionallyStopped := a.forceStopping
	a.forceStopping = false
	a.busy = false
	a.cancelPending = false
	a.server.SetJobID("")
	a.progressActive = false
	a.progressText = "已停止"
	if a.server.Running() {
		a.setState(stateReadyText, "ready")
	} else {
		a.setState(stateIdleText, "idle")
	}
	if intentionallyStopped {
		a.progressText = "已强制停止"
		a.appendLog("任务已因服务停止而中断。")
		return
	}
	a.appendLog("[失败] " + message)
	a.logVisible = true
	a.showError("生成失败", message)
}

func (a *app) showImage(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		a.appendLog("预览失败：" + err.Error())
		return
	}
	bitmap, err := ui.DecodeBitmap(data)
	if err != nil {
		a.appendLog("预览失败：" + err.Error())
		return
	}
	a.preview = bitmap
}

// ------------------------------------------------------------ 结果与预览

func (a *app) closePreview() {
	a.preview = nil
}

func (a *app) saveResult() {
	if a.resultPath == "" || !isFile(a.resultPath) {
		return
	}
	go func() {
		target, err := mygo.Dialog.Save(mygo.SaveDialogOptions{
			Parent:      a.win,
			Title:       "另存为",
			DefaultPath: filepath.Base(a.resultPath),
			Filters:     []mygo.FileFilter{{Name: "PNG 图片", Extensions: []string{"png"}}},
		})
		if err != nil || target == "" {
			return
		}
		data, err := os.ReadFile(a.resultPath)
		if err == nil {
			err = os.WriteFile(target, data, 0o644)
		}
		a.update(func() {
			if err != nil {
				a.appendLog("保存失败：" + err.Error())
			} else {
				a.appendLog("已保存到 " + target)
			}
		})
	}()
}

func (a *app) openResult() {
	if a.resultPath != "" && isFile(a.resultPath) {
		if err := mygo.Shell.OpenPath(a.resultPath); err != nil {
			a.showError("无法打开图片", err.Error())
		}
	}
}

func (a *app) openOutputFolder() {
	if err := os.MkdirAll(outputsDir, 0o755); err != nil {
		a.showError("无法打开输出目录", err.Error())
		return
	}
	if err := mygo.Shell.OpenPath(outputsDir); err != nil {
		a.showError("无法打开输出目录", err.Error())
	}
}

// showError 报告错误：有窗口时弹系统对话框（须从协程调用，对话框会阻塞），
// 测试里没有窗口就记进日志。
func (a *app) showError(title, message string) {
	if a.win == nil {
		a.appendLog("[" + title + "] " + message)
		return
	}
	go mygo.Dialog.Error(title, message)
}

// ------------------------------------------------------------ 窗口关闭

func (a *app) onClose(e *mygo.CloseEvent) {
	if a.busy && !a.closeConfirmed {
		e.PreventDefault()
		go func() {
			res, err := mygo.Dialog.Message(mygo.MessageOptions{
				Parent:  a.win,
				Type:    mygo.MessageWarning,
				Message: "正在生成，退出会中断本次生成。确定退出吗？",
				Buttons: []string{"退出", "取消"},
			})
			if err == nil && res.Button == 0 {
				a.update(func() { a.closeConfirmed = true })
				a.win.Close()
			}
		}()
		return
	}
	if a.server.Adopted() && !a.adoptedNotified {
		e.PreventDefault()
		go func() {
			_, _ = mygo.Dialog.Message(mygo.MessageOptions{
				Parent:  a.win,
				Message: "1234 端口上的服务并非本窗口启动，退出后它仍会占用显存与内存。\n如需释放，请结束该 sd-server.exe 进程。",
				Buttons: []string{"知道了"},
			})
			a.update(func() { a.adoptedNotified = true })
			a.win.Close()
		}()
	}
}
