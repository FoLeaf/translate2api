package main

// TransHub Go 版入口：配置、装配网关与后台、启动 HTTP 服务。

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

type appConfig struct {
	AppName         string
	DataDir         string
	Port            int
	AdminPassword   string
	SessionDays     int
	DoubaoUpstream  string
	BilibiliUpstream string

	DoubaoQueueWait int // 豆包入口排队等待上限（秒）
	DoubaoQueueCap  int // 豆包全局同时在网关内排队上限
	DoubaoKeyCap    int // 单 Key 同时在网关内排队上限（公平性）
	DoubaoTimeout   int // 豆包上游单次调用总超时（秒）
	DoubaoBatchMax  int // 单次上游调用最多段数
	BiliUpConc      int // B站上游并发信号量
	TrustedProxies  []string
}

var cfg appConfig
var store *Store
var secret string

func envStr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func loadConfig() appConfig {
	return appConfig{
		AppName:          "TransHub",
		DataDir:          envStr("TH_DATA_DIR", "/data"),
		Port:             envInt("TH_PORT", 8000),
		AdminPassword:    os.Getenv("ADMIN_PASSWORD"),
		SessionDays:      envInt("TH_SESSION_DAYS", 7),
		DoubaoUpstream:   strings.TrimRight(envStr("TH_DOUBAO_UPSTREAM", "https://www.doubao.com"), "/"),
		BilibiliUpstream: strings.TrimRight(envStr("TH_BILIBILI_UPSTREAM", "https://index-translate.bilibili.com"), "/"),
		DoubaoQueueWait:  envInt("TH_DOUBAO_QUEUE_WAIT", 12),
		DoubaoQueueCap:   envInt("TH_DOUBAO_QUEUE_CAP", 48),
		DoubaoKeyCap:     envInt("TH_DOUBAO_KEY_CAP", 12),
		DoubaoTimeout:    envInt("TH_DOUBAO_TIMEOUT", 45),
		DoubaoBatchMax:   envInt("TH_DOUBAO_BATCH_MAX", 8),
		BiliUpConc:       envInt("TH_BILIBILI_UPSTREAM_CONC", 8),
		TrustedProxies:   splitComma(envStr("TH_TRUSTED_PROXIES", "127.0.0.1")),
	}
}

func splitComma(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func main() {
	cfg = loadConfig()

	var err error
	store, err = OpenStore(cfg.DataDir)
	if err != nil {
		log.Fatalf("打开数据库失败: %v", err)
	}
	if msg, err := store.MigrateFrom(cfg.DataDir + "/transhub.db"); err != nil {
		log.Printf("[TransHub] 迁移检查失败: %v", err)
	} else if msg != "" {
		log.Printf("[TransHub] %s", msg)
	}

	secret, err = store.GetOrCreateSecret()
	if err != nil {
		log.Fatalf("初始化会话密钥失败: %v", err)
	}

	ensureInitialAdminPassword()
	newDoubaoService()
	newBiliService()
	go keyTouchLoop()

	mux := http.NewServeMux()
	registerGateway(mux)
	registerAdmin(mux)

	srv := &http.Server{
		Addr:              fmt.Sprintf(":%d", cfg.Port),
		Handler:           requestLog(mux),
		ReadHeaderTimeout: 20 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	log.Printf("[TransHub-Go] 监听 :%d，数据目录 %s", cfg.Port, cfg.DataDir)
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP 服务退出: %v", err)
		}
	}()
	waitSignal()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}

// requestLog 简易访问日志，格式对齐 uvicorn 风格。
func requestLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			next.ServeHTTP(w, r)
			return
		}
		rec := &statusRecorder{ResponseWriter: w, status: 200}
		start := time.Now()
		next.ServeHTTP(rec, r)
		log.Printf("%s - \"%s %s HTTP/1.1\" %d %s",
			clientIP(r), r.Method, r.URL.RequestURI(), rec.status,
			time.Since(start).Round(time.Millisecond))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// Flush 透传给底层 ResponseWriter，保证 SSE 流式刷新。
func (r *statusRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// clientIP 只信任可信代理链的 X-Forwarded-For，直连场景记 socket 地址。
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	trusted := false
	for _, p := range cfg.TrustedProxies {
		if host == p {
			trusted = true
			break
		}
	}
	if !trusted {
		return host
	}
	xff := r.Header.Get("X-Forwarded-For")
	if xff == "" {
		return host
	}
	return strings.TrimSpace(strings.Split(xff, ",")[0])
}

// keyTouchLoop 每 60 秒批量刷新使用过的 Key 的 last_used_at。
func keyTouchLoop() {
	seen := map[int64]time.Time{}
	for range time.Tick(60 * time.Second) {
		dirty := keyTouchSwap()
		for id := range dirty {
			if t, ok := seen[id]; !ok || time.Since(t) > 5*time.Minute {
				store.TouchAPIKey(id)
				seen[id] = time.Now()
			}
		}
	}
}

func writeJSON(w http.ResponseWriter, status int, v any, headers map[string]string) {
	for k, val := range headers {
		w.Header().Set(k, val)
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func ensureInitialAdminPassword() {
	if _, ok := store.GetSetting("admin_password_hash"); ok {
		return
	}
	if cfg.AdminPassword != "" {
		_ = store.SetSetting("admin_password_hash", HashPassword(cfg.AdminPassword))
		return
	}
	pw := "th-" + randTokenURLSafe(9)
	_ = store.SetSetting("admin_password_hash", HashPassword(pw))
	path := cfg.DataDir + "/initial_admin_password.txt"
	_ = os.WriteFile(path, []byte(pw+"\n"), 0o600)
	log.Printf("[TransHub-Go] 已生成随机管理员密码: %s（同时写入 %s，登录后请修改）", pw, path)
}
