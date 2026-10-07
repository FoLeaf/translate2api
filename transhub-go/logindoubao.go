package main

// 豆包扫码登录：容器内 Chromium + 截图流。
// 豆包 SSO 扫码接口被字节风控(bdms)保护，裸调 API 拿不到 token；
// accounts.doubao.com 是整页 SSO 登录页，默认展示「豆包 App 扫一扫」，
// 扫码确认后自动回跳 doubao.com 并落地登录 Cookie。
// 浏览器仅在登录期间存活，平时不占内存。

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/launcher"
	"github.com/go-rod/rod/lib/proto"
)

const (
	doubaoLoginURL     = "https://accounts.doubao.com/"
	doubaoLoginTimeout = 5 * time.Minute
	doubaoFrameEvery   = 1200 * time.Millisecond
)

var doubaoCookieMarkers = []string{"sessionid", "sid_tt", "uid_tt"}

type doubaoLoginFlow struct {
	mu      sync.Mutex
	status  string
	detail  string
	frame   []byte
	seq     int
	cancel  context.CancelFunc
	running bool
}

var doubaoFlow = &doubaoLoginFlow{}

func (f *doubaoLoginFlow) Start() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.running {
		return map[string]any{"ok": true, "mode": "frame", "detail": "登录会话进行中"}
	}
	ctx, cancel := context.WithTimeout(context.Background(), doubaoLoginTimeout)
	f.cancel = cancel
	f.running = true
	f.status = "waiting"
	f.detail = "正在启动浏览器…"
	f.frame = nil
	go f.run(ctx)
	return map[string]any{"ok": true, "mode": "frame", "detail": "浏览器启动中"}
}

func (f *doubaoLoginFlow) Poll() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return map[string]any{"status": f.status, "detail": f.detail}
}

func (f *doubaoLoginFlow) Frame() ([]byte, int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.frame, f.seq
}

func (f *doubaoLoginFlow) Cancel() {
	f.mu.Lock()
	if f.cancel != nil {
		f.cancel()
	}
	f.running = false
	f.status = "idle"
	f.detail = "已取消"
	f.mu.Unlock()
}

func (f *doubaoLoginFlow) setStatus(status, detail string) {
	f.mu.Lock()
	f.status = status
	f.detail = detail
	f.mu.Unlock()
}

func (f *doubaoLoginFlow) finish(status, detail string) {
	f.mu.Lock()
	f.status = status
	f.detail = detail
	f.running = false
	f.mu.Unlock()
}

func (f *doubaoLoginFlow) run(ctx context.Context) {
	bin := envStr("TH_CHROMIUM_BIN", "chromium")
	l := launcher.New().
		Bin(bin).
		Headless(true).
		Leakless(false). // alpine/musl 不兼容 leakless 辅助二进制
		Set("no-sandbox").
		Set("disable-dev-shm-usage").
		Set("disable-gpu").
		Set("mute-audio").
		Set("disable-blink-features", "AutomationControlled")
	wsURL, err := l.Launch()
	if err != nil {
		f.finish("error", "浏览器启动失败: "+err.Error()+"（可先用 Cookie 手动导入）")
		return
	}

	browser := rod.New().ControlURL(wsURL)
	if err := browser.Connect(); err != nil {
		f.finish("error", "连接浏览器失败: "+err.Error())
		return
	}
	defer func() { _ = browser.Close() }()

	page, err := browser.Page(proto.TargetCreateTarget{URL: doubaoLoginURL})
	if err != nil {
		f.finish("error", "打开登录页失败: "+err.Error())
		return
	}
	_ = page.SetViewport(1100, 860, 1, false)

	time.Sleep(4 * time.Second)
	f.setStatus("waiting", "请用豆包 App「扫一扫」扫描页面右侧二维码")

	t0 := time.Now()
	for time.Since(t0) < doubaoLoginTimeout {
		if ctx.Err() != nil {
			f.finish("idle", "已取消")
			return
		}
		if png, err := page.Screenshot(false, &proto.PageCaptureScreenshot{
			Format: proto.PageCaptureScreenshotFormatPng,
		}); err == nil {
			f.mu.Lock()
			f.frame = png
			f.seq++
			f.mu.Unlock()
		}

		if f.harvest(page) {
			return
		}
		time.Sleep(doubaoFrameEvery)
	}
	f.finish("expired", "超时未扫码，请重新发起登录")
}

// harvest 检查 Cookie 是否已落地，命中则等回跳稳定后收割入库。
func (f *doubaoLoginFlow) harvest(page *rod.Page) bool {
	cookies := collectCookies(page)
	if !hasMarker(cookies) {
		return false
	}
	time.Sleep(2500 * time.Millisecond)
	cookies = append(cookies, collectCookies(page)...)

	seen := map[string]bool{}
	var parts []string
	var names []string
	for _, c := range cookies {
		key := c.Name + "|" + c.Domain
		if seen[key] {
			continue
		}
		seen[key] = true
		parts = append(parts, c.Name+"="+c.Value)
		names = append(names, c.Name)
	}
	_ = store.PutCredential("doubao", map[string]any{
		"cookie":       strings.Join(parts, "; "),
		"cookie_names": names,
		"captured_at":  time.Now().Unix(),
	}, "default", "active")
	f.finish("confirmed", "登录成功，豆包 Cookie 已自动保存")
	return true
}

func collectCookies(page *rod.Page) []*proto.NetworkCookie {
	cookies, err := page.Cookies("https://www.doubao.com", "https://accounts.doubao.com")
	if err != nil {
		return nil
	}
	return cookies
}

func hasMarker(cookies []*proto.NetworkCookie) bool {
	for _, c := range cookies {
		for _, m := range doubaoCookieMarkers {
			if c.Name == m {
				return true
			}
		}
	}
	return false
}
