package main

import (
	"fmt"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/egoist/mygo/ui"
)

const (
	stateIdleText  = "服务未启动"
	stateReadyText = "服务就绪"
)

// 浅色中性灰：白色面板、浅灰工作区、再深一档的画布，层级靠背景明度区分，不用渐变和发光。
// 唯一的颜色是赭橙，只给生成按钮、进度和选中的画幅；画面里的颜色留给图。
var (
	colCanvas   = ui.Hex("#F3F2EF") // 工作区底色
	colPanel    = ui.Hex("#FFFFFF") // 顶栏与左栏
	colStage    = ui.Hex("#E9E8E4") // 画布，比工作区深一档，让图跳出来
	colCard     = ui.Hex("#FFFFFF") // 输入卡片
	colFloat    = ui.Hex("#FFFFFF") // 浮在图上的工具条
	colControl  = ui.Hex("#F1F0ED")
	colHover    = ui.Hex("#E8E6E2")
	colPressed  = ui.Hex("#DDDBD6")
	colLine     = ui.Hex("#E3E1DC")
	colLineHi   = ui.Hex("#C9C6C0")
	colText     = ui.Hex("#1D1C1A")
	colText2    = ui.Hex("#57534D")
	colText3    = ui.Hex("#8C877F")
	colAccent   = ui.Hex("#C4510F")
	colAccentHi = ui.Hex("#B0470C")
	colAccentLo = ui.Hex("#983C09")
	colOnAccent = ui.Hex("#FFFFFF")
	colDanger   = ui.Hex("#C8372D")
	colShadow   = ui.RGBA(30, 25, 20, 0.08)
)

const fontNum = "Cascadia Mono, Consolas, monospace"

// 圆角三档：小件 4，控件 6，大面 10。
const (
	radiusS = 4
	radiusM = 6
	radiusL = 10
)

const fadeDuration = 180 * time.Millisecond

func svgIcon(body string) *ui.SVG {
	return ui.MustParseSVG([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">` + body + `</svg>`))
}

var (
	icPlus     = svgIcon(`<path d="M12 5V19M5 12H19"/>`)
	icClose    = svgIcon(`<path d="M6 6L18 18M18 6L6 18"/>`)
	icLeft     = svgIcon(`<path d="M15 6L9 12L15 18"/>`)
	icRight    = svgIcon(`<path d="M9 6L15 12L9 18"/>`)
	icFolder   = svgIcon(`<path d="M3 6.5H9L11 8.5H21V19H3Z"/>`)
	icLog      = svgIcon(`<path d="M4 7L9 12L4 17M12 17H20"/>`)
	icDownload = svgIcon(`<path d="M12 4V15M7 10L12 15L17 10M5 20H19"/>`)
	icOpen     = svgIcon(`<path d="M14 4H20V10M20 4L11 13M18 14V20H4V6H10"/>`)
	icShuffle  = svgIcon(`<path d="M16 4H20V8M4 20L20 4M20 16V20H16M15 15L20 20M4 4L9 9"/>`)
	icImage    = svgIcon(`<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 16L8 11L13 16M14 14L16.5 11.5L21 16"/><circle cx="15.5" cy="8.5" r="1.5"/>`)
	icBan      = svgIcon(`<circle cx="12" cy="12" r="9"/><path d="M8 12H16"/>`)
)

func stateColor(kind string) ui.Color {
	switch kind {
	case "busy":
		return colAccent
	case "ready":
		return ui.Hex("#3F8A4A")
	case "error":
		return colDanger
	default:
		return colText3
	}
}

func (a *app) setTheme(c *ui.Context) {
	t := *ui.LightTheme()
	t.Background = colCanvas
	t.Surface = colControl
	t.SurfaceHover = colHover
	t.SurfacePressed = colPressed
	t.Border = colLine
	t.Text = colText
	t.TextMuted = colText3
	t.Accent = colAccent
	t.AccentHover = colAccentHi
	t.AccentPressed = colAccentLo
	t.AccentText = colOnAccent
	t.Danger = colDanger
	t.Warning = colAccent
	t.Success = ui.Hex("#3F8A4A")
	t.Selection = colAccent.Alpha(0.20)
	t.Focus = colAccent.Alpha(0.45)
	t.Scrollbar = ui.RGBA(0, 0, 0, 0.22)
	t.Radius = radiusM
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
		a.header(c)
		ui.Row(c).Grow(1).AlignItems(ui.Stretch).Children(func() {
			a.rail(c)
			a.studio(c)
		})
	})
}

// ------------------------------------------------------------ 通用小部件

// press 按下时下沉 1 像素。MyGo 没有缩放变换，用位移代替缩小。
func press(b *ui.Element) {
	if b.Pressed() {
		b.Top(1)
	}
}

// fadeIn 元素出现时从透明淡入；换了 Key 的元素会重新淡入一次。
func fadeIn(c *ui.Context, e *ui.Element) *ui.Element {
	mounted := ui.Local(e, "mounted", func() bool { return false })
	target := float32(0)
	if *mounted {
		target = 1
	} else {
		*mounted = true
		c.Invalidate()
	}
	return e.Opacity(e.Animate("fade", target, fadeDuration))
}

// button 次要按钮：控件底色，悬停抬亮一级。
func button(c *ui.Context, ic *ui.SVG, label string, active, disabled bool) *ui.Element {
	b := ui.ButtonBase(c).Gap(6).Padding(6, 12).Radius(radiusM).Background(colControl).
		TextColor(colText2).Disabled(disabled)
	switch {
	case disabled:
		b.TextColor(colText3)
	case active:
		b.Background(colPressed).TextColor(colText)
	case b.Pressed():
		b.Background(colPressed).TextColor(colText)
	case b.Hovered():
		b.Background(colHover).TextColor(colText)
	}
	press(b)
	b.Children(func() {
		if ic != nil {
			ui.Icon(c, ic).FontSize(14)
		}
		ui.Text(c, label).FontSize(12.5).SingleLine()
	})
	return b
}

// iconButton 只有图标的方形按钮，名字给提示框和读屏。
func iconButton(c *ui.Context, ic *ui.SVG, label string, active bool) *ui.Element {
	b := ui.ButtonBase(c).Size(32, 32).Radius(radiusM).Label(label).Tooltip(label).TextColor(colText2)
	switch {
	case active:
		b.Background(colPressed).TextColor(colText)
	case b.Pressed():
		b.Background(colPressed).TextColor(colText)
	case b.Hovered():
		b.Background(colHover).TextColor(colText)
	}
	press(b)
	b.Children(func() { ui.Icon(c, ic).FontSize(16) })
	return b
}

func sectionTitle(c *ui.Context, title, aside string) {
	ui.Row(c).AlignItems(ui.Center).Children(func() {
		ui.Text(c, title).FontSize(12).Bold().TextColor(colText2)
		ui.Spacer(c)
		if aside != "" {
			ui.Text(c, aside).Font(fontNum).FontSize(11).TextColor(colText3)
		}
	})
}

// ------------------------------------------------------------ 顶栏

func (a *app) header(c *ui.Context) {
	ui.Row(c).Height(52).Padding(0, 16).Gap(12).AlignItems(ui.Center).
		Background(colPanel).BorderWidth(0, 0, 1, 0).BorderColor(colLine).Children(func() {
		ui.Row(c).Gap(8).AlignItems(ui.End).Children(func() {
			ui.Text(c, "Qwen Image").FontSize(15).Bold()
			ui.Text(c, "本地创作台").FontSize(11).TextColor(colText3).Margin(0, 0, 2, 0)
		})
		ui.Box(c).Size(1, 20).Margin(0, 8).Background(colLine)
		a.modeSwitch(c)
		ui.Spacer(c)
		ui.Row(c).Gap(7).AlignItems(ui.Center).Children(func() {
			ui.Box(c).Size(7, 7).Radius(999).Background(stateColor(a.stateKind))
			ui.Text(c, a.stateText).FontSize(12.5).TextColor(colText2)
		})
		a.serviceButton(c)
		ui.Box(c).Size(1, 20).Background(colLine)
		if iconButton(c, icLog, "运行记录", a.logVisible).Clicked() {
			a.logVisible = !a.logVisible
		}
		if iconButton(c, icFolder, "输出目录", false).Clicked() {
			a.openOutputFolder()
		}
	})
}

// modeSwitch 文生图 / 图像编辑：选中段抬亮一级，不用强调色。
func (a *app) modeSwitch(c *ui.Context) {
	idx := 0
	if a.mode == "edit" {
		idx = 1
	}
	seg := ui.SegmentedBase(c, &idx, 2)
	seg.Track.Padding(3).Gap(2).Radius(radiusM + 3).Background(colControl).Label("创作模式").Children(func() {
		for i, name := range []string{"文生图", "图像编辑"} {
			s := seg.Segment(i).Padding(5, 14).Radius(radiusM).Cursor(ui.CursorPointer)
			color := colText3
			switch {
			case i == idx:
				s.Background(colPanel).Shadow(0, 1, 2, 0, ui.RGBA(0, 0, 0, 0.12))
				color = colText
			case s.Hovered():
				color = colText2
			}
			s.Children(func() {
				text := ui.Text(c, name).FontSize(12.5).TextColor(color)
				if i == idx {
					text.Bold()
				}
			})
		}
	})
	if idx == 1 {
		a.mode = "edit"
	} else {
		a.mode = "txt2img"
	}
}

func (a *app) serviceButton(c *ui.Context) {
	switch {
	case a.serviceStarting:
		button(c, nil, "启动中…", false, true)
	case a.server.Running():
		if button(c, nil, "停止服务", false, !a.server.Stoppable()).Clicked() {
			a.stopService(false)
		}
	default:
		if button(c, nil, "启动服务", false, false).Clicked() {
			a.startService()
		}
	}
}

// ------------------------------------------------------------ 左侧参数栏

func (a *app) rail(c *ui.Context) {
	ui.Column(c).Width(292).Shrink(0).Background(colPanel).
		BorderWidth(0, 1, 0, 0).BorderColor(colLine).Children(func() {
		ui.Scroll(c).Grow(1).Children(func() {
			ui.Column(c).Padding(20, 18, 16, 18).Gap(26).Children(func() {
				a.aspectSection(c)
				a.paramSection(c)
			})
		})
		a.resourceBlock(c)
	})
}

func (a *app) aspectSection(c *ui.Context) {
	ui.Column(c).Gap(12).Children(func() {
		sectionTitle(c, "画幅", strings.TrimSpace(a.width)+" × "+strings.TrimSpace(a.height))
		selected := a.selectedPreset()
		ui.Grid(c).Columns(4).GapX(6).GapY(6).Children(func() {
			for i, p := range a.activePresets() {
				a.presetTile(c, p, i == selected)
			}
		})
		ui.Row(c).Gap(10).AlignItems(ui.Center).Padding(4, 0, 0, 0).Children(func() {
			// 文字也能点；setHD 需要按旧档找出当前比例，所以开关改的是副本
			label := ui.Column(c).Grow(1).Gap(2).Cursor(ui.CursorPointer)
			label.Children(func() {
				ui.Text(c, "高清档").FontSize(12.5)
				ui.Text(c, "边长约 1.5 倍，单张约 2~4 分钟").FontSize(11).TextColor(colText3)
			})
			hd := a.hd
			if ui.Switch(c, &hd).Label("高清档开关").Changed() {
				a.setHD(hd)
			} else if label.Clicked() {
				a.setHD(!a.hd)
			}
		})
	})
}

// presetTile 一格宽高比：画出该比例的矩形，选中时矩形填成强调色。
func (a *app) presetTile(c *ui.Context, p sizePreset, active bool) {
	tile := ui.Box(c).Height(60).Radius(radiusM).Cursor(ui.CursorPointer).Focusable().
		Label("尺寸 " + p.label).Background(colControl)
	hover := tile.Hovered()
	switch {
	case active:
		tile.Background(colPressed)
	case hover:
		tile.Background(colHover)
	}
	press(tile)
	tile.Draw(func(pt *ui.Painter, r ui.Rect) {
		color := colText3
		switch {
		case active:
			color = colText
		case hover:
			color = colText2
		}
		const box = float32(20)
		scale := min(box/float32(p.width), box/float32(p.height))
		w := max(float32(7), float32(p.width)*scale)
		h := max(float32(7), float32(p.height)*scale)
		cx, cy := r.X+r.W/2, r.Y+22
		rect := ui.Rect{X: cx - w/2, Y: cy - h/2, W: w, H: h}
		if active {
			pt.Fill(rect, colAccent, 2)
		} else {
			pt.Stroke(rect, color, 2, 1.4)
		}
		label := ui.Span{Text: p.label, Font: fontNum, Size: 10.5, Color: color}
		lw, lh := pt.MeasureText(0, label)
		pt.RichText(r.X+(r.W-lw)/2, r.Y+r.H-lh-6, 0, label)
	})
	if tile.Clicked() {
		a.choosePreset(p)
	}
}

// paramSection 参数像相机的拍摄信息：左边名称，右边等宽数字。
func (a *app) paramSection(c *ui.Context) {
	ui.Column(c).Gap(10).Children(func() {
		sectionTitle(c, "参数", "")
		changed := false
		ui.Column(c).Gap(6).Children(func() {
			changed = paramRow(c, "宽", &a.width, nil) || changed
			changed = paramRow(c, "高", &a.height, nil) || changed
			changed = paramRow(c, "步数", &a.steps, nil) || changed
			changed = paramRow(c, "引导强度", &a.cfg, nil) || changed
			changed = paramRow(c, "种子", &a.seed, func() {
				if iconButton(c, icShuffle, "随机种子", strings.TrimSpace(a.seed) == "-1").Size(30, 30).Clicked() {
					a.seed = "-1"
					changed = true
				}
			}) || changed
		})
		if changed {
			a.refreshParams()
		}
		if a.paramError != "" {
			ui.Text(c, a.paramError).FontSize(11.5).TextColor(colDanger)
		} else {
			ui.Text(c, "宽高需为 128 的倍数；种子 -1 为随机").FontSize(11.5).TextColor(colText3)
		}
		ui.Text(c, a.sizeText).FontSize(11.5).TextColor(colText2)
	})
}

func paramRow(c *ui.Context, label string, value *string, trailing func()) bool {
	changed := false
	ui.Row(c).Gap(8).AlignItems(ui.Center).Children(func() {
		ui.Text(c, label).FontSize(12).TextColor(colText2).Width(64)
		input := ui.TextInput(c, value).Grow(1).Font(fontNum).FontSize(13).Padding(6, 10).
			Radius(radiusM).Background(colControl).Border(1, ui.Transparent).Label(label)
		if input.Focused() {
			input.Border(1, colAccent.Alpha(0.6))
		}
		changed = input.Changed()
		if trailing != nil {
			trailing()
		}
	})
	return changed
}

func (a *app) resourceBlock(c *ui.Context) {
	ui.Column(c).Gap(4).Padding(12, 18, 14, 18).BorderWidth(1, 0, 0, 0).BorderColor(colLine).Children(func() {
		ui.Text(c, a.vramText).Font(fontNum).FontSize(11).TextColor(colText3)
		ui.Text(c, a.ramText).Font(fontNum).FontSize(11).TextColor(colText3)
	})
}

// ------------------------------------------------------------ 工作区：画布 + 输入卡片

func (a *app) studio(c *ui.Context) {
	ui.Column(c).Grow(1).Padding(16, 20).Gap(12).Children(func() {
		ui.Box(c).Grow(1).MinHeight(180).Radius(radiusL).Clip().Background(colStage).
			Border(1, colLine).Children(func() {
			switch {
			case a.busy || a.forceStopping:
				a.generatingPanel(c)
			case a.preview != nil:
				a.previewPanel(c)
			default:
				a.idlePanel(c)
			}
		})
		a.composer(c)
		if a.logVisible {
			a.logPane(c)
		}
	})
}

// currentAspect 当前填写的宽高比，填错时按方形。
func (a *app) currentAspect() float32 {
	w, werr := parseInt(a.width)
	h, herr := parseInt(a.height)
	if werr != nil || herr != nil || w <= 0 || h <= 0 {
		return 1
	}
	return float32(w) / float32(h)
}

// frameSize 把宽高比为 aspect 的画框放进 maxW × maxH。
func frameSize(aspect, maxW, maxH float32) (float32, float32) {
	if aspect <= 0 {
		aspect = 1
	}
	if maxW/maxH > aspect {
		return maxH * aspect, maxH
	}
	return maxW, maxW / aspect
}

// idlePanel 还没有图：按当前画幅画一个虚线画框，改画幅时它跟着变。
func (a *app) idlePanel(c *ui.Context) {
	panel := ui.Column(c).Key("idle").Fill().Center().Gap(12).Padding(24)
	fadeIn(c, panel).Children(func() {
		fw, fh := frameSize(a.currentAspect(), 320, 260)
		ui.Box(c).Size(fw, fh).Center().Radius(radiusS).Border(1, colLineHi).BorderStyle(ui.BorderDashed).
			Children(func() {
				ui.Text(c, strings.TrimSpace(a.width)+" × "+strings.TrimSpace(a.height)).
					Font(fontNum).FontSize(12).TextColor(colText3)
			})
		hint := "在下方写下画面描述，Ctrl+Enter 生成；首次生成会先加载模型"
		if a.mode == "edit" {
			hint = "在下方添加参考图并写下修改；图片也可以直接拖进窗口"
		}
		ui.Text(c, hint).FontSize(12).TextColor(colText3).TextAlign(ui.Center).MaxWidth(440)
	})
}

// generatingPanel 生成中：同一个画框里秒数在走，底边一道强调色细线按预计耗时推进。
func (a *app) generatingPanel(c *ui.Context) {
	c.After(time.Second)
	elapsed := time.Since(a.genStarted).Seconds()
	progress := float32(0)
	if a.genEstimate > 0 {
		progress = float32(min(elapsed/a.genEstimate, 0.95))
	}
	panel := ui.Column(c).Key("generating").Fill().Center().Gap(14).Padding(24)
	fadeIn(c, panel).Children(func() {
		fw, fh := frameSize(a.genAspect, 320, 260)
		ui.Box(c).Size(fw, fh).Center().Radius(radiusS).Clip().Background(colCard).Border(1, colLine).
			DrawOver(func(p *ui.Painter, r ui.Rect) {
				p.Fill(ui.Rect{X: r.X, Y: r.Y + r.H - 2, W: r.W * progress, H: 2}, colAccent, 0)
			}).Children(func() {
			ui.Row(c).Gap(5).AlignItems(ui.End).Children(func() {
				ui.Textf(c, "%.0f", elapsed).Font(fontNum).FontSize(44).FontFeatures("tnum")
				ui.Text(c, "秒").FontSize(13).TextColor(colText3).Margin(0, 0, 9, 0)
			})
		})
		caption := a.progressText
		if a.genEstimate > 0 {
			caption += fmt.Sprintf("；预计约 %.0f 秒", a.genEstimate)
		}
		ui.Text(c, caption).FontSize(12).TextColor(colText2).TextAlign(ui.Center).MaxWidth(440)
		cancelText := "取消任务"
		if a.forceStopping {
			cancelText = "正在停止…"
		} else if a.cancelPending {
			cancelText = "请求取消中…"
		}
		if button(c, nil, cancelText, false, a.cancelPending || a.forceStopping).Clicked() {
			a.cancelJob()
		}
		ui.Text(c, "等待期间可以继续改描述和参数").FontSize(11.5).TextColor(colText3)
	})
}

// previewPanel 结果铺满画布，操作在右上角，文件名与耗时在左下角。
func (a *app) previewPanel(c *ui.Context) {
	panel := ui.Box(c).Key("preview:" + a.resultPath).Fill().Padding(16)
	fadeIn(c, panel).Children(func() {
		ui.Image(c, a.preview).Fill().Fit(ui.Contain)
		ui.Row(c).Absolute().Top(12).Right(12).Gap(2).Padding(3).Radius(radiusM+3).
			Background(colFloat).Border(1, colLine).Shadow(0, 2, 8, 0, colShadow).Children(func() {
			if button(c, icOpen, "打开图片", false, false).Background(ui.Transparent).Clicked() {
				a.openResult()
			}
			if button(c, icDownload, "另存为…", false, false).Background(ui.Transparent).Clicked() {
				a.saveResult()
			}
			if button(c, icClose, "关闭预览", false, false).Background(ui.Transparent).Clicked() {
				a.closePreview()
			}
		})
		ui.Text(c, a.progressText).Absolute().Bottom(12).Left(12).Padding(4, 9).Radius(radiusS).
			Background(colFloat).Font(fontNum).FontSize(11).TextColor(colText2).SingleLine()
	})
}

// composer 输入卡片：描述是主角，参考图、负面提示词和生成按钮围着它。
func (a *app) composer(c *ui.Context) {
	negOpen := ui.Local(c.Root(), "negOpen", func() bool { return false })
	focused := false
	card := ui.Column(c).WidthPercent(100).MaxWidth(900).AlignSelf(ui.Center).Shrink(0).
		Padding(12, 14, 10, 14).Gap(10).Radius(radiusL).Background(colCard).Shadow(0, 2, 10, 0, colShadow)
	card.Children(func() {
		if a.mode == "edit" {
			a.refStrip(c)
		}
		placeholder := "描述你想要的画面：主体、环境、光线、镜头，越具体越好"
		if a.mode == "edit" {
			placeholder = "描述要怎么改，用「图1」「图2」指代上面的参考图"
		}
		area := ui.TextAreaBase(c, &a.prompt).Height(72).FontSize(14).LineHeight(1.6).
			Placeholder(placeholder).Label("画面描述")
		focused = area.Focused()

		if *negOpen {
			ui.Row(c).Gap(8).AlignItems(ui.Center).Padding(6, 10).Radius(radiusM).Background(colControl).
				Children(func() {
					ui.Text(c, "不想出现").FontSize(12).TextColor(colText3)
					ui.TextInputBase(c, &a.negative).Grow(1).FontSize(12.5).
						Placeholder("例如：模糊、水印、多余的手指").Label("负面提示词内容")
				})
		}

		ui.Row(c).Gap(8).AlignItems(ui.Center).Children(func() {
			negLabel := "负面提示词"
			if !*negOpen && strings.TrimSpace(a.negative) != "" {
				negLabel = "负面提示词（已填）"
			}
			if button(c, icBan, negLabel, *negOpen, false).Clicked() {
				*negOpen = !*negOpen
			}
			ratio := "自定义"
			if i := a.selectedPreset(); i >= 0 {
				ratio = a.activePresets()[i].label
			}
			ui.Text(c, fmt.Sprintf("%s · %s×%s", ratio, strings.TrimSpace(a.width), strings.TrimSpace(a.height))).
				Font(fontNum).FontSize(11.5).TextColor(colText3)
			ui.Spacer(c)
			ui.Textf(c, "%d 字", utf8.RuneCountInString(a.prompt)).FontSize(11).TextColor(colText3)
			ui.Text(c, "Ctrl+Enter").Font(fontNum).FontSize(10.5).TextColor(colText3).
				Padding(2, 6).Radius(radiusS).Border(1, colLine)
			a.generateButton(c)
		})
	})
	if focused {
		card.Border(1, colAccent.Alpha(0.45))
	} else {
		card.Border(1, colLine)
	}
}

func (a *app) generateEnabled() bool {
	return !a.busy && !a.serviceStarting && a.paramsValid && !a.forceStopping
}

// generateButton 全界面唯一的实心强调色按钮。
func (a *app) generateButton(c *ui.Context) {
	label := "生成图像"
	switch {
	case a.forceStopping:
		label = "正在停止…"
	case a.busy:
		label = "正在生成…"
	case a.mode == "edit":
		label = "开始编辑"
	}
	enabled := a.generateEnabled()
	b := ui.ButtonBase(c).Padding(8, 20).Radius(radiusM).Disabled(!enabled)
	switch {
	case !enabled:
		b.Background(colControl).TextColor(colText3)
	case b.Pressed():
		b.Background(colAccentLo).TextColor(colOnAccent)
	case b.Hovered():
		b.Background(colAccentHi).TextColor(colOnAccent)
	default:
		b.Background(colAccent).TextColor(colOnAccent)
	}
	press(b)
	b.Children(func() { ui.Text(c, label).FontSize(13.5).Bold().SingleLine() })
	if b.Clicked() {
		a.generate()
	}
}

// ------------------------------------------------------------ 参考图

// refStrip 参考图缩略图条：编号就是提示词里的「图1、图2」，悬停或选中时可调顺序、移除。
func (a *app) refStrip(c *ui.Context) {
	hint, hintErr := a.modeHint()
	hintColor := colText3
	if hintErr {
		hintColor = colDanger
		if !a.editWarnLogged {
			a.editWarnLogged = true
			a.appendLog("提示：当前服务未加载视觉塔，编辑模式生成时会自动重启服务。")
		}
	}
	ui.Column(c).Gap(8).Children(func() {
		ui.Row(c).Gap(8).AlignItems(ui.Center).Children(func() {
			ui.Text(c, "参考图").FontSize(12).Bold().TextColor(colText2)
			ui.Text(c, hint).FontSize(11).TextColor(hintColor).SingleLine().Grow(1)
			if len(a.refPaths) > 0 {
				clearAll := ui.Text(c, "全部清除").FontSize(11).TextColor(colText3).Cursor(ui.CursorPointer)
				if clearAll.Hovered() {
					clearAll.TextColor(colText)
				}
				if clearAll.Clicked() {
					a.clearRefs()
				}
			}
		})
		if len(a.refPaths) == 0 {
			a.refDropzone(c)
			return
		}
		ui.ScrollHorizontal(c).Height(74).Children(func() {
			ui.Row(c).Gap(8).Padding(2).Children(func() {
				// 按钮的动作在循环结束后再执行，免得边遍历边改列表
				var action func()
				for i, path := range a.refPaths {
					if act := a.refTile(c, i, path); act != nil {
						action = act
					}
				}
				add := ui.ButtonBase(c).Size(68, 68).Radius(radiusM).Border(1, colLineHi).
					BorderStyle(ui.BorderDashed).Label("添加参考图").Tooltip("添加参考图").TextColor(colText3)
				if add.Hovered() {
					add.TextColor(colText).Background(colControl)
				}
				press(add)
				add.Children(func() { ui.Icon(c, icPlus).FontSize(18) })
				if add.Clicked() {
					a.pickRefs()
				}
				if action != nil {
					action()
				}
			})
		})
	})
}

func (a *app) refDropzone(c *ui.Context) {
	zone := ui.ButtonBase(c).Height(64).Gap(10).Radius(radiusM).
		Border(1, colLineHi).BorderStyle(ui.BorderDashed).TextColor(colText3)
	if zone.Hovered() {
		zone.Background(colControl).TextColor(colText2)
	}
	zone.Children(func() {
		ui.Icon(c, icImage).FontSize(18)
		ui.Text(c, "拖入图片，或点击选择参考图").FontSize(12.5)
		ui.Text(c, "可多张，顺序对应提示词里的「图1、图2」").FontSize(11).TextColor(colText3)
	})
	if zone.Clicked() {
		a.pickRefs()
	}
}

// refTile 一张参考图的缩略图，返回本帧被点中的动作。
func (a *app) refTile(c *ui.Context, i int, path string) (action func()) {
	selected := i == a.refSelected
	tile := ui.Box(c).Key(path).Size(68, 68).Shrink(0).Radius(radiusM).Clip().Cursor(ui.CursorPointer).
		Label(fmt.Sprintf("参考图 %d", i+1)).Tooltip(filepath.Base(path)).Background(colControl)
	if selected {
		tile.Border(1.5, colText2)
	}
	hovered := tile.Hovered()
	last := len(a.refPaths) - 1
	tile.Children(func() {
		if bitmap := a.refThumbs[path]; bitmap != nil {
			ui.Image(c, bitmap).Fill().Fit(ui.Cover)
		} else {
			ui.Column(c).Fill().Center().Padding(6).Children(func() {
				ui.Text(c, filepath.Base(path)).FontSize(9.5).TextColor(colText3).MaxLines(3)
			})
		}
		ui.Text(c, fmt.Sprintf("图%d", i+1)).Absolute().Top(4).Left(4).Padding(0, 5).Radius(radiusS).
			Background(ui.RGBA(0, 0, 0, 0.62)).FontSize(10).TextColor(colOnAccent)
		if hovered || selected {
			ui.Row(c).Absolute().Left(0).Right(0).Bottom(0).Height(24).Justify(ui.SpaceEvenly).
				Background(ui.RGBA(255, 255, 255, 0.92)).BorderWidth(1, 0, 0, 0).BorderColor(colLine).Children(func() {
				if i > 0 && tinyButton(c, icLeft, "左移").Clicked() {
					action = func() { a.refSelected = i; a.moveRef(-1) }
				}
				if tinyButton(c, icClose, "移除").Clicked() {
					action = func() { a.refSelected = i; a.removeRef() }
				}
				if i < last && tinyButton(c, icRight, "右移").Clicked() {
					action = func() { a.refSelected = i; a.moveRef(1) }
				}
			})
		}
	})
	if tile.Clicked() && action == nil {
		action = func() { a.refSelected = i }
	}
	return action
}

func tinyButton(c *ui.Context, ic *ui.SVG, label string) *ui.Element {
	b := ui.ButtonBase(c).Size(20, 20).Radius(radiusS).Label(label).Tooltip(label).TextColor(colText2)
	if b.Hovered() {
		b.Background(colHover).TextColor(colText)
	}
	press(b)
	b.Children(func() { ui.Icon(c, ic).FontSize(12) })
	return b
}

// ------------------------------------------------------------ 运行记录

func (a *app) logPane(c *ui.Context) {
	ui.Column(c).Height(150).Shrink(0).WidthPercent(100).MaxWidth(900).AlignSelf(ui.Center).
		Radius(radiusL).Background(colPanel).Border(1, colLine).Clip().Children(func() {
		ui.Row(c).Padding(8, 10, 4, 14).AlignItems(ui.Center).Children(func() {
			ui.Text(c, "运行记录").FontSize(11.5).Bold().TextColor(colText3)
			ui.Spacer(c)
			if tinyButton(c, icClose, "收起记录").Clicked() {
				a.logVisible = false
			}
		})
		ui.Scroll(c).Grow(1).TrackScroll(&a.logScroll).Children(func() {
			ui.Column(c).Padding(2, 14, 10, 14).Gap(2).Children(func() {
				for _, line := range a.logs {
					ui.Text(c, line).Font(fontNum).FontSize(11).TextColor(colText2).Selectable()
				}
			})
		})
	})
}
