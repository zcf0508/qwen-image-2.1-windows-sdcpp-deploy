package main

import (
	"os"
	"path/filepath"
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
		name                        string
		w, h, steps, cfg, seed      string
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
	for _, text := range []string{"Qwen Image", "本地创作台", stateIdleText, "文生图", "图像编辑", "画面描述", "生成图像", "等待第一张图像"} {
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

func TestViewParamValidation(t *testing.T) {
	a := newApp()
	a.width = "1000"
	a.refreshParams()
	tt := ui.NewTester(a.view, 1280, 800)
	if !tt.HasText("宽和高必须是 128 的倍数，避免分块接缝") {
		t.Errorf("param error not shown; texts %q", tt.Texts())
	}
	if a.generateEnabled() {
		t.Error("generate must stay disabled while params are invalid")
	}
}

func TestViewEditMode(t *testing.T) {
	a := newApp()
	tt := ui.NewTester(a.view, 1280, 800)
	if tt.HasText("选择参考图…") {
		t.Error("ref panel must stay hidden in txt2img mode")
	}
	if err := tt.Click("图像编辑"); err != nil {
		t.Fatal(err)
	}
	if a.mode != "edit" {
		t.Fatalf("mode %q after clicking radio", a.mode)
	}
	if !tt.HasText("选择参考图…") {
		t.Errorf("ref panel not shown in edit mode; texts %q", tt.Texts())
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
