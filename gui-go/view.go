package main

import (
	"fmt"
	"path/filepath"
	"unicode/utf8"

	"github.com/egoist/mygo/ui"
)

const (
	stateIdleText  = "● 服务未启动"
	stateReadyText = "● 服务就绪"
)

// 暖中性灰底 + 单一铜色强调，沿用 Python 版的视觉规范
var (
	colPanel      = ui.Hex("#FFFEFC")
	colBorder     = ui.Hex("#C8BFB7")
	colMuted      = ui.Hex("#665D57")
	colAccentSoft = ui.Hex("#FBEEE1")
	colLogBG      = ui.Hex("#1C1917")
	colLogFG      = ui.Hex("#D8D2CB")
)

func stateColor(kind string) ui.Color {
	switch kind {
	case "busy":
		return ui.Hex("#B45309")
	case "ready":
		return ui.Hex("#15803D")
	case "error":
		return ui.Hex("#B91C1C")
	default:
		return ui.Hex("#8B5E3C")
	}
}

func (a *app) setTheme(c *ui.Context) {
	t := *ui.LightTheme()
	t.Background = ui.Hex("#F4F1EE")
	t.Surface = ui.Hex("#E5DFD9")
	t.SurfaceHover = ui.Hex("#D8CFC7")
	t.SurfacePressed = ui.Hex("#CCC1B8")
	t.Border = colBorder
	t.Text = ui.Hex("#1F1B18")
	t.TextMuted = colMuted
	t.Accent = ui.Hex("#A8520A")
	t.AccentHover = ui.Hex("#8F4508")
	t.AccentPressed = ui.Hex("#8F4508")
	t.Danger = ui.Hex("#B91C1C")
	t.Success = ui.Hex("#15803D")
	t.Selection = ui.RGBA(168, 82, 10, 0.18)
	t.Focus = ui.RGBA(168, 82, 10, 0.55)
	t.Radius = 2
	t.Font = "Microsoft YaHei UI, Segoe UI"
	t.FontSize = 13
	c.SetTheme(&t)
}

func (a *app) view(c *ui.Context) {
	a.setTheme(c)

	if c.Shortcut(ui.Cmd, ui.KeyEnter) && a.generateEnabled() {
		a.generate()
	}

	ui.Column(c).Fill().Children(func() {
		a.statusBar(c)
		ui.Split(c, &a.splitPos,
			func() { a.controlsPane(c) },
			func() { a.previewPane(c) },
		).Grow(1).Padding(0, 12, 0, 12)
		if a.logVisible {
			a.logPane(c)
		}
	})
}

// ------------------------------------------------------------ 顶部状态栏

func (a *app) statusBar(c *ui.Context) {
	t := c.Theme()
	ui.Row(c).Padding(10, 12, 6, 12).Gap(10).AlignItems(ui.Center).Children(func() {
		ui.Text(c, "Qwen Image").FontSize(19).Bold()
		ui.Text(c, "本地创作台").TextColor(t.TextMuted)
		ui.Text(c, a.stateText).TextColor(stateColor(a.stateKind)).Bold()
		ui.Spacer(c)
		ui.Text(c, a.vramText).TextColor(t.TextMuted).Font("monospace").FontSize(12)
		ui.Text(c, a.ramText).TextColor(t.TextMuted).Font("monospace").FontSize(12).Padding(0, 0, 0, 14)
	})
}

// ------------------------------------------------------------ 左侧控制面板

func (a *app) controlsPane(c *ui.Context) {
	ui.Column(c).Fill().Children(func() {
		ui.Scroll(c).Grow(1).Children(func() {
			ui.Column(c).Gap(10).Padding(0, 8, 0, 0).Children(func() {
				a.serviceBar(c)
				a.inputSection(c)
				a.sizeSection(c)
				a.paramsSection(c)
			})
		})
		a.actionBar(c)
	})
}

func (a *app) serviceBar(c *ui.Context) {
	ui.Row(c).Gap(6).AlignItems(ui.Center).Padding(0, 0, 2, 0).Children(func() {
		ui.Text(c, "推理服务").TextColor(c.Theme().TextMuted)
		ui.Spacer(c)
		startLabel := "启动服务"
		if a.serviceStarting {
			startLabel = "启动中…"
		}
		if ui.Button(c, startLabel).Disabled(a.serviceStarting || a.server.Running()).Clicked() {
			a.startService()
		}
		if ui.Button(c, "停止").Disabled(!(a.server.Stoppable() && !a.serviceStarting)).Clicked() {
			a.stopService(false)
		}
	})
}

// section 画一个带标题的卡片，替代 Tk 的 LabelFrame。
func section(c *ui.Context, title string, body func()) {
	t := c.Theme()
	ui.Column(c).Border(1, t.Border).Background(colPanel).Radius(4).Children(func() {
		ui.Text(c, title).FontSize(11).Bold().TextColor(t.TextMuted).Padding(7, 8, 0, 8)
		ui.Column(c).Padding(4, 8, 8, 8).Gap(6).Children(body)
	})
}

func (a *app) inputSection(c *ui.Context) {
	t := c.Theme()
	section(c, "输入", func() {
		ui.Row(c).Gap(6).Children(func() {
			ui.Radio(c, &a.mode, "txt2img", "文生图").Grow(1)
			ui.Radio(c, &a.mode, "edit", "图像编辑").Grow(1)
		})
		hint, hintErr := a.modeHint()
		hintColor := t.TextMuted
		if hintErr {
			hintColor = t.Danger
			if !a.editWarnLogged {
				a.editWarnLogged = true
				a.appendLog("提示：当前服务未加载视觉塔，编辑模式生成时会自动重启服务。")
			}
		}
		ui.Text(c, hint).TextColor(hintColor).FontSize(12)

		if a.mode == "edit" {
			a.refPanel(c)
		}

		ui.Row(c).Padding(6, 0, 0, 0).Children(func() {
			ui.Text(c, "画面描述").Bold()
			ui.Spacer(c)
			ui.Textf(c, "%d 字", utf8.RuneCountInString(a.prompt)).TextColor(t.TextMuted).FontSize(12)
		})
		ui.TextArea(c, &a.prompt).Height(88)
		ui.Text(c, "负面提示词（可留空）").TextColor(t.TextMuted).FontSize(12).Padding(4, 0, 0, 0)
		ui.TextInput(c, &a.negative)
	})
}

func (a *app) refPanel(c *ui.Context) {
	t := c.Theme()
	ui.Row(c).Gap(6).AlignItems(ui.Center).Children(func() {
		if ui.Button(c, "选择参考图…").Clicked() {
			a.pickRefs()
		}
		if ui.Button(c, "全部清除").Disabled(len(a.refPaths) == 0).Clicked() {
			a.refPaths = nil
			a.refSelected = -1
		}
		refText := "未选择"
		if len(a.refPaths) > 0 {
			refText = fmt.Sprintf("已选 %d 张", len(a.refPaths))
		}
		ui.Text(c, refText).TextColor(t.TextMuted).FontSize(12)
	})
	if len(a.refPaths) > 0 {
		ui.List(c, &a.refList, len(a.refPaths), func(i int) {
			ui.Textf(c, "%d. %s", i+1, filepath.Base(a.refPaths[i])).SingleLine().Padding(3, 6)
		}).Height(72).Border(1, t.Border).Radius(2)
		ui.Row(c).Gap(5).Children(func() {
			i := a.refSelected
			if ui.Button(c, "上移").Disabled(i <= 0).Clicked() {
				a.moveRef(-1)
			}
			if ui.Button(c, "下移").Disabled(i < 0 || i >= len(a.refPaths)-1).Clicked() {
				a.moveRef(1)
			}
			if ui.Button(c, "移除").Disabled(i < 0).Clicked() {
				a.removeRef()
			}
		})
	}
}

// ------------------------------------------------------------ 尺寸预设

func (a *app) sizeSection(c *ui.Context) {
	t := c.Theme()
	section(c, "尺寸预设", func() {
		if ui.Checkbox(c, &a.hd, "高清档（边长约 1.5 倍，单张约 2~4 分钟）").Changed() {
			a.refreshParams()
		}
		selected := a.selectedPreset()
		ui.Row(c).Gap(2).Children(func() {
			for i, p := range a.activePresets() {
				a.presetTile(c, p, i == selected)
			}
		})
		ui.Text(c, a.sizeText).TextColor(t.TextMuted).FontSize(12)
	})
}

// presetTile 一格宽高比预设：画出该比例的缩略矩形，点一下切换尺寸。
func (a *app) presetTile(c *ui.Context, p sizePreset, active bool) {
	t := c.Theme()
	tile := ui.Box(c).Grow(1).Height(62).Radius(4).Cursor(ui.CursorPointer).Label("尺寸 " + p.label)
	if active {
		tile.Background(colAccentSoft).Border(1, t.Accent)
	} else if tile.Hovered() {
		tile.Background(t.Surface)
	}
	tile.Draw(func(pt *ui.Painter, r ui.Rect) {
		color := t.TextMuted
		fill := colPanel
		if active {
			color = t.Accent
			fill = t.Accent
		}
		const box = float32(24)
		scale := min(box/float32(p.width), box/float32(p.height))
		w := max(float32(7), float32(p.width)*scale)
		h := max(float32(7), float32(p.height)*scale)
		cx, cy := r.X+r.W/2, r.Y+22
		rect := ui.Rect{X: cx - w/2, Y: cy - h/2, W: w, H: h}
		pt.Fill(rect, fill, 0)
		pt.Stroke(rect, color, 0, 1)

		label := ui.Span{Text: p.label, Size: 11, Color: color}
		lw, lh := pt.MeasureText(0, label)
		pt.RichText(r.X+(r.W-lw)/2, r.Y+r.H-lh-5, 0, label)
	})
	if tile.Clicked() {
		a.choosePreset(p)
	}
}

// ------------------------------------------------------------ 参数

func (a *app) paramsSection(c *ui.Context) {
	t := c.Theme()
	section(c, "参数", func() {
		changed := false
		ui.Grid(c).Columns(3).GapX(8).GapY(6).Children(func() {
			changed = a.paramField(c, "宽", &a.width) || changed
			changed = a.paramField(c, "高", &a.height) || changed
			changed = a.paramField(c, "步数", &a.steps) || changed
			changed = a.paramField(c, "引导", &a.cfg) || changed
			changed = a.paramField(c, "种子", &a.seed) || changed
		})
		if changed {
			a.refreshParams()
		}
		ui.Text(c, "宽高需为 128 的倍数 · 种子 -1 为随机").TextColor(t.TextMuted).FontSize(12).Padding(4, 0, 0, 0)
		if a.paramError != "" {
			ui.Text(c, a.paramError).TextColor(t.Danger).FontSize(12)
		}
	})
}

func (a *app) paramField(c *ui.Context, label string, value *string) bool {
	changed := false
	ui.Row(c).Gap(6).AlignItems(ui.Center).Children(func() {
		ui.Text(c, label).TextColor(c.Theme().TextMuted).FontSize(12)
		if ui.TextInput(c, value).Grow(1).Font("monospace").Changed() {
			changed = true
		}
	})
	return changed
}

// ------------------------------------------------------------ 底部操作

func (a *app) generateEnabled() bool {
	return !a.busy && !a.serviceStarting && a.paramsValid && !a.forceStopping
}

func (a *app) actionBar(c *ui.Context) {
	generateText := "生成图像"
	switch {
	case a.forceStopping:
		generateText = "正在停止…"
	case a.busy:
		generateText = "正在生成…"
	case a.mode == "edit":
		generateText = "开始编辑"
	}
	cancelText := "取消任务"
	if a.forceStopping {
		cancelText = "停止中…"
	} else if a.cancelPending {
		cancelText = "请求中…"
	}
	ui.Row(c).Gap(8).Padding(10, 0, 0, 0).Children(func() {
		if ui.PrimaryButton(c, generateText).Grow(1).Disabled(!a.generateEnabled()).Clicked() {
			a.generate()
		}
		if ui.Button(c, cancelText).Disabled(!(a.busy && !a.cancelPending && !a.forceStopping)).Clicked() {
			a.cancelJob()
		}
	})
}

// ------------------------------------------------------------ 右侧预览

func (a *app) previewPane(c *ui.Context) {
	t := c.Theme()
	ui.Column(c).Fill().Padding(0, 0, 0, 8).Children(func() {
		ui.Box(c).Grow(1).Border(1, t.Border).Background(colPanel).Radius(4).Clip().Children(func() {
			if a.preview != nil {
				ui.Image(c, a.preview).Fill().Fit(ui.Contain)
			} else {
				ui.Column(c).Fill().Center().Gap(6).Children(func() {
					ui.Text(c, "◇").FontSize(28).TextColor(t.Accent)
					ui.Text(c, "等待第一张图像").FontSize(15).Bold()
					ui.Text(c, "填写左侧画面描述，按 Ctrl+Enter 开始").TextColor(t.TextMuted)
				})
			}
		})
		ui.Row(c).Gap(6).AlignItems(ui.Center).Padding(10, 0, 0, 0).Children(func() {
			ui.Column(c).Grow(1).Gap(4).Children(func() {
				ui.Text(c, a.progressText).TextColor(t.TextMuted).Font("monospace").FontSize(12).SingleLine()
				if a.progressActive {
					ui.Progress(c, -1)
				}
			})
			if ui.Button(c, "关闭预览").Disabled(a.preview == nil).Clicked() {
				a.closePreview()
			}
			if ui.Button(c, "打开图片").Disabled(a.resultPath == "").Clicked() {
				a.openResult()
			}
			if ui.Button(c, "另存为…").Disabled(a.resultPath == "").Clicked() {
				a.saveResult()
			}
			logLabel := "运行记录"
			if a.logVisible {
				logLabel = "收起记录"
			}
			if ui.Button(c, logLabel).Clicked() {
				a.logVisible = !a.logVisible
			}
			if ui.Button(c, "输出目录").Clicked() {
				a.openOutputFolder()
			}
		})
	})
}

// ------------------------------------------------------------ 运行记录

func (a *app) logPane(c *ui.Context) {
	ui.Box(c).Height(128).Background(colLogBG).Children(func() {
		ui.Scroll(c).Fill().TrackScroll(&a.logScroll).Children(func() {
			ui.Column(c).Padding(6, 8).Children(func() {
				for _, line := range a.logs {
					ui.Text(c, line).Font("monospace").FontSize(11).TextColor(colLogFG).Selectable()
				}
			})
		})
	})
}
