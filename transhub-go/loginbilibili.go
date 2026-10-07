package main

// B站扫码登录：passport.bilibili.com 公开 QR API，无风控，纯 HTTP 实现。

import (
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	qrcode "github.com/skip2/go-qrcode"
)

const (
	biliQRGenURL  = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
	biliQRPollURL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
)

type biliQRFlow struct {
	mu        sync.Mutex
	qrcodeKey string
	createdAt time.Time
	status    string
	detail    string
}

var biliFlow = &biliQRFlow{}

func (f *biliQRFlow) httpClient() *http.Client {
	return &http.Client{Timeout: 20 * time.Second}
}

func (f *biliQRFlow) Start() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.qrcodeKey != "" && time.Since(f.createdAt) < 3*time.Minute {
		return map[string]any{"ok": true, "mode": "qr_png", "detail": "二维码已生成，请继续扫码"}
	}
	resp, err := f.httpClient().Get(biliQRGenURL + "?source=transhub")
	if err != nil {
		return map[string]any{"ok": false, "detail": "generate 请求失败: " + err.Error()}
	}
	defer resp.Body.Close()
	var body struct {
		Data struct {
			URL       string `json:"url"`
			QRCodeKey string `json:"qrcode_key"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil || body.Data.URL == "" {
		return map[string]any{"ok": false, "detail": "generate 未返回二维码"}
	}
	png, err := qrcode.Encode(body.Data.URL, qrcode.Medium, 384)
	if err != nil {
		return map[string]any{"ok": false, "detail": "二维码渲染失败: " + err.Error()}
	}
	f.qrcodeKey = body.Data.QRCodeKey
	f.createdAt = time.Now()
	f.status = "waiting"
	f.detail = ""
	return map[string]any{
		"ok":     true,
		"mode":   "qr_png",
		"qr_png": "data:image/png;base64," + base64.StdEncoding.EncodeToString(png),
		"detail": "请用B站 App 扫码",
	}
}

func (f *biliQRFlow) Poll() map[string]any {
	f.mu.Lock()
	key, created := f.qrcodeKey, f.createdAt
	f.mu.Unlock()
	if key == "" {
		return map[string]any{"status": "error", "detail": "尚未发起登录"}
	}
	if time.Since(created) > 180*time.Second {
		return map[string]any{"status": "expired", "detail": "二维码已过期，请重新发起"}
	}
	resp, err := f.httpClient().Get(biliQRPollURL + "?qrcode_key=" + url.QueryEscape(key) + "&source=transhub")
	if err != nil {
		return map[string]any{"status": "waiting", "detail": "poll 请求异常，继续等待"}
	}
	defer resp.Body.Close()
	var body struct {
		Data struct {
			Code    int    `json:"code"`
			URL     string `json:"url"`
			Message string `json:"message"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return map[string]any{"status": "waiting", "detail": "poll 返回异常，继续等待"}
	}
	switch body.Data.Code {
	case 86101:
		return map[string]any{"status": "waiting", "detail": "等待扫码"}
	case 86090:
		return map[string]any{"status": "scanned", "detail": "已扫码，请在手机上确认"}
	case 0:
		if body.Data.URL != "" {
			u, err := url.Parse(body.Data.URL)
			if err == nil {
				q := u.Query()
				var parts []string
				for _, name := range []string{"SESSDATA", "bili_jct", "DedeUserID",
					"DedeUserID__ckMd5", "Expires"} {
					if v := q.Get(name); v != "" {
						parts = append(parts, name+"="+v)
					}
				}
				_ = store.PutCredential("bilibili", map[string]any{
					"cookie":      strings.Join(parts, "; "),
					"uid":         q.Get("DedeUserID"),
					"captured_at": time.Now().Unix(),
				}, "default", "active")
				f.mu.Lock()
				f.qrcodeKey = ""
				f.mu.Unlock()
				return map[string]any{"status": "confirmed",
					"detail": "登录成功（uid=" + q.Get("DedeUserID") + "），Cookie 已自动保存"}
			}
		}
		return map[string]any{"status": "expired", "detail": "回调地址解析失败，请重试"}
	}
	msg := body.Data.Message
	if msg == "" {
		msg = "二维码已失效，请重新发起"
	}
	return map[string]any{"status": "expired", "detail": msg}
}

func (f *biliQRFlow) Cancel() {
	f.mu.Lock()
	f.qrcodeKey = ""
	f.mu.Unlock()
}
