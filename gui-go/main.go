package main

import (
	"log"
	"time"

	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
)

func main() {
	a := newApp()
	mygo.App.WhenReady(func() {
		win := mygo.NewWindow(mygo.WindowOptions{
			Title:     "Qwen-Image-2.1",
			Width:     1280,
			Height:    800,
			MinWidth:  1040,
			MinHeight: 680,
			// Opens where the user left it last time.
			StateKey: "main",
			// The window shows the interface MyGo draws, not a web page.
			Content: ui.View(a.view),
		})
		a.win = win
		// 图片可直接拖进窗口，按顺序加进参考图列表
		win.OnFileDrop(func(e *mygo.FileDropEvent) {
			a.update(func() { a.filesDropped(e.Paths) })
		})
		win.OnClose(a.onClose)
		// 无论正常关闭还是异常退出，都确保服务进程被结束
		win.OnClosed(func() {
			close(a.done)
			a.server.Stop()
		})
		go a.monitorLoop()
	})
	if err := mygo.App.Run(); err != nil {
		log.Fatal(err)
	}
}

// monitorLoop 每 2 秒刷新一次显存与内存读数。
func (a *app) monitorLoop() {
	for {
		vram, ram := resourceText()
		a.update(func() { a.vramText, a.ramText = vram, ram })
		select {
		case <-a.done:
			return
		case <-time.After(2 * time.Second):
		}
	}
}
