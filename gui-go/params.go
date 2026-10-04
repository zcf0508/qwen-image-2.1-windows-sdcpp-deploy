package main

import (
	"encoding/base64"
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
)

// 宽高比预设：尺寸必须是 128 的整数倍。VAE 分块解码的接缝落在瓦片边界，
// 非 128 倍数会在接缝处出现亮度台阶（实测 1280x864 跳变 9.39 灰度级，
// 128 倍数尺寸仅 0.15~0.28）。
type sizePreset struct {
	label         string
	width, height int
}

var sizePresets = []sizePreset{
	{"1:1", 1024, 1024},
	{"4:3", 1024, 768},
	{"3:4", 768, 1024},
	{"3:2", 1152, 768},
	{"2:3", 768, 1152},
	{"16:9", 1152, 640},
	{"9:16", 640, 1152},
}

// 高清档：边长约为标准档的 1.5 倍，像素量 1.4~2.4 MP，单张约 2~4 分钟。
// 同样落在 128 网格上；16:9 与 9:16 取 2048×1152，比例正好 1.778。
var sizePresetsHD = []sizePreset{
	{"1:1", 1536, 1536},
	{"4:3", 1536, 1152},
	{"3:4", 1152, 1536},
	{"3:2", 1536, 1024},
	{"2:3", 1024, 1536},
	{"16:9", 2048, 1152},
	{"9:16", 1152, 2048},
}

// 单张耗时粗估系数，秒/百万像素。取自三次实测：1024x1024（1.05 MP / 89 秒）、
// 1536x1024（1.57 MP / 136 秒）、1408x2048（2.88 MP / 296 秒）
const secondsPerMP = 95.0

type genParams struct {
	width, height, steps int
	cfg                  float64
	seed                 int
}

var errNotNumber = errors.New("宽、高、步数、引导和种子都必须是数字")

func parseGenerationParameters(widthText, heightText, stepsText, cfgText, seedText string) (genParams, error) {
	var p genParams
	var err error
	if p.width, err = strconv.Atoi(strings.TrimSpace(widthText)); err != nil {
		return p, errNotNumber
	}
	if p.height, err = strconv.Atoi(strings.TrimSpace(heightText)); err != nil {
		return p, errNotNumber
	}
	if p.steps, err = strconv.Atoi(strings.TrimSpace(stepsText)); err != nil {
		return p, errNotNumber
	}
	if p.cfg, err = strconv.ParseFloat(strings.TrimSpace(cfgText), 64); err != nil {
		return p, errNotNumber
	}
	if p.seed, err = strconv.Atoi(strings.TrimSpace(seedText)); err != nil {
		return p, errNotNumber
	}
	if p.width <= 0 || p.height <= 0 {
		return p, errors.New("宽和高必须大于 0")
	}
	if p.width%128 != 0 || p.height%128 != 0 {
		return p, errors.New("宽和高必须是 128 的倍数，避免分块接缝")
	}
	if p.steps <= 0 {
		return p, errors.New("步数必须大于 0")
	}
	if math.IsNaN(p.cfg) || math.IsInf(p.cfg, 0) || p.cfg < 0 {
		return p, errors.New("引导强度必须是大于等于 0 的有限数字")
	}
	return p, nil
}

// genRequest 是一次生成任务的完整快照，从界面状态取出后交给后台协程。
type genRequest struct {
	prompt   string
	negative string
	editing  bool
	refPaths []string
	params   genParams
}

func (r genRequest) estimateSeconds() float64 {
	return float64(r.params.width) * float64(r.params.height) / 1e6 * secondsPerMP
}

// buildPayload 组装提交给 sd-server 的 JSON。图像编辑时参考图按顺序编码，
// increase_ref_index 让每张参考图拿到递增的位置索引，模型据此区分它们，
// 顺序即提示词里的「图一、图二」。
func (r genRequest) buildPayload() (map[string]any, error) {
	seed := r.params.seed
	if seed < 0 {
		seed = -1
	}
	payload := map[string]any{
		"prompt":          r.prompt,
		"negative_prompt": r.negative,
		"width":           r.params.width,
		"height":          r.params.height,
		"seed":            seed,
		"batch_count":     1,
		"sample_params": map[string]any{
			"sample_method": "euler",
			"sample_steps":  r.params.steps,
			"guidance":      map[string]any{"txt_cfg": r.params.cfg},
		},
		"output_format": "png",
		// 解码分块显式开启。服务端不会像 sd-cli 那样在解码失败后自动降级，
		// 尺寸超过约 1 MP 就必须自己开，否则整张大图失败。实测代价接近于零
		// （1024 尺寸下分块 5.72 秒，整体解码 5.74 秒）。
		"vae_tiling_params": map[string]any{"enabled": true},
	}
	if r.editing {
		images := make([]string, 0, len(r.refPaths))
		for _, path := range r.refPaths {
			data, err := os.ReadFile(path)
			if err != nil {
				return nil, fmt.Errorf("读取参考图失败：%w", err)
			}
			images = append(images, "data:image/png;base64,"+base64.StdEncoding.EncodeToString(data))
		}
		payload["ref_images"] = images
		payload["increase_ref_index"] = true
	}
	return payload, nil
}
