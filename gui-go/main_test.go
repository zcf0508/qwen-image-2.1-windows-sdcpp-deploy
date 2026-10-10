package main

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/egoist/mygo/ui"
)

func TestParseGenerationParameters(t *testing.T) {
	p, err := parseGenerationParameters("1024", "1152", "40", "1.0", "-1")
	if err != nil {
		t.Fatalf("valid params rejected: %v", err)
	}
	if p.width != 1024 || p.height != 1152 || p.steps != 40 || p.cfg != 1.0 || p.seed != -1 {
		t.Errorf("unexpected params: %+v", p)
	}

	cases := []struct {
		name                   string
		w, h, steps, cfg, seed string
	}{
		{"not a number", "abc", "1024", "40", "1.0", "-1"},
		{"negative width", "-128", "1024", "40", "1.0", "-1"},
		{"not multiple of 128", "1000", "1024", "40", "1.0", "-1"},
		{"height not multiple of 128", "1024", "100", "40", "1.0", "-1"},
		{"zero steps", "1024", "1024", "0", "1.0", "-1"},
		{"negative cfg", "1024", "1024", "40", "-0.5", "-1"},
	}
	for _, tc := range cases {
		if _, err := parseGenerationParameters(tc.w, tc.h, tc.steps, tc.cfg, tc.seed); err == nil {
			t.Errorf("%s: expected error", tc.name)
		}
	}
}

func TestBuildPayloadTxt2Img(t *testing.T) {
	req := genRequest{
		prompt:   "一只猫",
		negative: "模糊",
		params:   genParams{width: 1024, height: 1024, steps: 40, cfg: 1.0, seed: -5},
	}
	payload, err := req.buildPayload()
	if err != nil {
		t.Fatal(err)
	}
	if payload["prompt"] != "一只猫" || payload["negative_prompt"] != "模糊" {
		t.Errorf("prompts wrong: %v", payload)
	}
	if payload["seed"] != -1 {
		t.Errorf("negative seed should normalize to -1, got %v", payload["seed"])
	}
	if payload["width"] != 1024 || payload["height"] != 1024 || payload["batch_count"] != 1 {
		t.Errorf("dimensions wrong: %v", payload)
	}
	sp, ok := payload["sample_params"].(map[string]any)
	if !ok || sp["sample_method"] != "euler" || sp["sample_steps"] != 40 {
		t.Errorf("sample_params wrong: %v", sp)
	}
	guidance, ok := sp["guidance"].(map[string]any)
	if !ok || guidance["txt_cfg"] != 1.0 {
		t.Errorf("guidance wrong: %v", guidance)
	}
	tiling, ok := payload["vae_tiling_params"].(map[string]any)
	if !ok || tiling["enabled"] != true {
		t.Errorf("vae tiling must stay enabled: %v", tiling)
	}
	if _, has := payload["ref_images"]; has {
		t.Error("txt2img payload must not carry ref_images")
	}
}

func TestBuildPayloadEdit(t *testing.T) {
	dir := t.TempDir()
	img := filepath.Join(dir, "ref.png")
	if err := os.WriteFile(img, []byte("fakepng"), 0o644); err != nil {
		t.Fatal(err)
	}
	req := genRequest{
		prompt:   "把图一变成黑白",
		editing:  true,
		refPaths: []string{img},
		params:   genParams{width: 1024, height: 1024, steps: 40, cfg: 1.0, seed: 42},
	}
	payload, err := req.buildPayload()
	if err != nil {
		t.Fatal(err)
	}
	if payload["seed"] != 42 {
		t.Errorf("explicit seed must be kept, got %v", payload["seed"])
	}
	if payload["increase_ref_index"] != true {
		t.Error("edit payload must set increase_ref_index")
	}
	images, ok := payload["ref_images"].([]string)
	if !ok || len(images) != 1 {
		t.Fatalf("ref_images wrong: %v", payload["ref_images"])
	}
	want := "data:image/png;base64,ZmFrZXBuZw=="
	if images[0] != want {
		t.Errorf("ref image encoding: got %q, want %q", images[0], want)
	}
}

func TestBuildPayloadMissingRef(t *testing.T) {
	req := genRequest{
		prompt:   "edit",
		editing:  true,
		refPaths: []string{filepath.Join(t.TempDir(), "nope.png")},
		params:   genParams{width: 128, height: 128, steps: 1, cfg: 1, seed: 0},
	}
	if _, err := req.buildPayload(); err == nil {
		t.Error("missing ref image must fail")
	}
}

// 视图在没有窗口的测试器里渲染，点击与输入和真人操作走同一条路。
func TestViewInitial(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	for _, text := range []string{"Qwen Image", "本地创作台", stateIdleText, "文生图", "图像编辑", "画面描述", "生成图像", "Ctrl+Enter 生成"} {
		if !tt.HasText(text) {
			t.Errorf("initial view missing %q; texts %q", text, tt.Texts())
		}
	}
}

func TestViewPresetTiles(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	if err := tt.Click("尺寸 16:9"); err != nil {
		t.Fatal(err)
	}
	if a.width != "1152" || a.height != "640" {
		t.Errorf("after clicking 16:9: %sx%s", a.width, a.height)
	}
	if !tt.HasText("当前 1152 × 640") {
		t.Errorf("size text not updated; texts %q", tt.Texts())
	}
}

// 切换高清档要保留选中的比例，而不是让宽高停在旧档的尺寸上。
func TestSetHDKeepsAspect(t *testing.T) {
	a := newApp()
	a.choosePreset(sizePreset{"16:9", 1152, 640})
	a.setHD(true)
	if a.width != "2048" || a.height != "1152" {
		t.Fatalf("16:9 in HD: %sx%s, want 2048x1152", a.width, a.height)
	}
	if i := a.selectedPreset(); i < 0 || a.activePresets()[i].label != "16:9" {
		t.Errorf("16:9 must stay selected in HD, got index %d", i)
	}
	a.setHD(false)
	if a.width != "1152" || a.height != "640" {
		t.Errorf("back to standard: %sx%s, want 1152x640", a.width, a.height)
	}

	// 自定义尺寸不属于任何预设，切档时原样保留
	a.width, a.height = "896", "1280"
	a.refreshParams()
	a.setHD(true)
	if a.width != "896" || a.height != "1280" || !a.hd {
		t.Errorf("custom size must be kept: %sx%s hd=%v", a.width, a.height, a.hd)
	}
}

func TestViewHDSwitchKeepsTile(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	if err := tt.Click("尺寸 3:4"); err != nil {
		t.Fatal(err)
	}
	if err := tt.Click("高清档"); err != nil {
		t.Fatal(err)
	}
	if !a.hd || a.width != "1152" || a.height != "1536" {
		t.Errorf("after HD switch: hd=%v %sx%s, want 1152x1536", a.hd, a.width, a.height)
	}
	if !tt.HasText("1152 × 1536") {
		t.Errorf("view not updated; texts %q", tt.Texts())
	}
}

func TestViewParamValidation(t *testing.T) {
	a := newApp()
	a.width = "1000"
	a.refreshParams()
	tt := ui.NewTester(a.view, 1280, 800)
	if !tt.HasText("宽和高必须是 128 的倍数，避免分块接缝") {
		t.Errorf("param error not shown; texts %q", tt.Texts())
	}
	if !tt.HasText("自定义 · 1000×1024") {
		t.Errorf("composer size chip not updated; texts %q", tt.Texts())
	}
	if a.generateEnabled() {
		t.Error("generate must stay disabled while params are invalid")
	}
}

func TestViewEditMode(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	if tt.HasText("拖入图片，或点击选择参考图") {
		t.Error("ref strip must stay hidden in txt2img mode")
	}
	if err := tt.Click("图像编辑"); err != nil {
		t.Fatal(err)
	}
	if a.mode != "edit" {
		t.Fatalf("mode %q after clicking segment", a.mode)
	}
	if !tt.HasText("拖入图片，或点击选择参考图") || !tt.HasText("开始编辑") {
		t.Errorf("edit mode not reflected; texts %q", tt.Texts())
	}
	if err := tt.Click("文生图"); err != nil {
		t.Fatal(err)
	}
	if a.mode != "txt2img" {
		t.Errorf("mode %q after switching back", a.mode)
	}
}

// 缩略图上的按钮作用于它所在的那一张，而不是之前选中的那一张。
func TestViewRefTileActions(t *testing.T) {
	a := newApp()
	a.addRefs([]string{`C:\a.png`, `C:\b.png`, `C:\c.png`})
	a.refSelected = 2
	tt := ui.NewTester(a.view, 1280, 800)
	// 选中第 3 张时它的操作条可见；左移后应排到第 2 位，且仍选中
	if err := tt.Click("左移"); err != nil {
		t.Fatalf("%v; texts %q", err, tt.Texts())
	}
	want := []string{`C:\a.png`, `C:\c.png`, `C:\b.png`}
	if !slices.Equal(a.refPaths, want) || a.refSelected != 1 {
		t.Fatalf("after move left: %v, selected %d", a.refPaths, a.refSelected)
	}
	if err := tt.Click("移除"); err != nil {
		t.Fatal(err)
	}
	if want := []string{`C:\a.png`, `C:\b.png`}; !slices.Equal(a.refPaths, want) {
		t.Errorf("after remove: %v", a.refPaths)
	}
	if _, ok := a.refThumbs[`C:\c.png`]; ok {
		t.Error("removed ref must drop its thumbnail")
	}
	if err := tt.Click("参考图 1"); err != nil {
		t.Fatal(err)
	}
	if a.refSelected != 0 {
		t.Errorf("clicking tile 1 must select it, selected %d", a.refSelected)
	}
}

func TestViewNegativeToggle(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	if _, ok := tt.Find("负面提示词"); !ok {
		t.Fatalf("negative toggle missing; texts %q", tt.Texts())
	}
	if err := tt.Click("负面提示词"); err != nil {
		t.Fatal(err)
	}
	if !tt.HasText("不想出现") {
		t.Errorf("negative input not opened; texts %q", tt.Texts())
	}
	a.negative = "水印"
	if err := tt.Click("负面提示词"); err != nil {
		t.Fatal(err)
	}
	if tt.HasText("不想出现") || !tt.HasText("负面提示词（已填）") {
		t.Errorf("closed toggle must show filled state; texts %q", tt.Texts())
	}
}

func TestViewIdleFrameFollowsAspect(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	if err := tt.Click("尺寸 9:16"); err != nil {
		t.Fatal(err)
	}
	if !tt.HasText("640 × 1152") {
		t.Errorf("idle frame label not updated; texts %q", tt.Texts())
	}
}

func TestRefListOperations(t *testing.T) {
	a := newApp()
	if added := a.addRefs([]string{`C:\a.png`, `C:\b.png`, `C:\a.png`}); added != 2 {
		t.Fatalf("added %d, want 2 (duplicates skipped)", added)
	}
	if a.mode != "edit" {
		t.Error("adding refs must switch to edit mode")
	}
	a.refSelected = 0
	a.moveRef(1)
	if a.refPaths[0] != `C:\b.png` || a.refSelected != 1 {
		t.Errorf("after move: %v, selected %d", a.refPaths, a.refSelected)
	}
	a.removeRef()
	if len(a.refPaths) != 1 || a.refPaths[0] != `C:\b.png` {
		t.Errorf("after remove: %v", a.refPaths)
	}
}

func TestFilesDropped(t *testing.T) {
	dir := t.TempDir()
	img := filepath.Join(dir, "drop.png")
	txt := filepath.Join(dir, "note.txt")
	os.WriteFile(img, []byte("x"), 0o644)
	os.WriteFile(txt, []byte("x"), 0o644)
	a := newApp()
	a.filesDropped([]string{img, txt})
	if len(a.refPaths) != 1 || a.refPaths[0] != img {
		t.Errorf("refs after drop: %v", a.refPaths)
	}
	found := false
	for _, line := range a.logs {
		if line == "已忽略非图片内容：note.txt" {
			found = true
		}
	}
	if !found {
		t.Errorf("ignored-file log missing: %v", a.logs)
	}
}

func TestGenerateValidation(t *testing.T) {
	a := newApp()
	a.generate() // prompt 为空
	if a.busy {
		t.Error("empty prompt must not start a job")
	}
	a.prompt = "一只猫"
	a.mode = "edit"
	a.generate() // 编辑模式没有参考图
	if a.busy {
		t.Error("edit mode without refs must not start a job")
	}
}
