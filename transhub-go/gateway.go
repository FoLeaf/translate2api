package main

// 对外翻译网关：与 Python 版路径、鉴权语义、响应格式完全一致。
// - POST /doubao/translate、/doubao、/doubao/{key}/translate、/doubao/{key}  DeepLX 协议
// - ANY  /bilibili/{path...}  OpenAI 兼容透传
// - 豆包入口排队（全局 + 单 Key 双上限，超时回 429 带 Retry-After）

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"
)

func corsHeaders(extra map[string]string) map[string]string {
	h := map[string]string{
		"Access-Control-Allow-Origin":  "*",
		"Access-Control-Allow-Headers": "*",
		"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
	}
	for k, v := range extra {
		h[k] = v
	}
	return h
}

func applyCORS(w http.ResponseWriter, extra map[string]string) {
	for k, v := range corsHeaders(extra) {
		w.Header().Set(k, v)
	}
}

// ------------------------------------------------------------- 鉴权

type keyIdentity struct {
	ID   int64
	Name string
}

// checkKey 返回 (nil, nil) 表示网关完全开放（一把 Key 都没有）。
func checkKey(r *http.Request, pathKey string) (*keyIdentity, *denyResponse) {
	if !store.KeyEnabledExists() {
		return nil, nil
	}
	token := pathKey
	if token == "" {
		token = parseAuthorization(r.Header.Get("Authorization"))
	}
	id, name, ok := store.VerifyAPIKey(token)
	if !ok {
		return nil, &denyResponse{status: 401, body: map[string]any{
			"code":    401,
			"message": "需要 API Key：请在后台创建，并以 Authorization: Bearer <key> 携带",
		}}
	}
	markKeyTouch(id)
	return &keyIdentity{ID: id, Name: name}, nil
}

type denyResponse struct {
	status int
	body   map[string]any
}

// keyTouch 热点 Key 的 last_used_at 由后台协程定期批量刷新。
var keyTouches struct {
	mu sync.Mutex
	m  map[int64]struct{}
}

func init() { keyTouches.m = map[int64]struct{}{} }

func markKeyTouch(id int64) {
	keyTouches.mu.Lock()
	keyTouches.m[id] = struct{}{}
	keyTouches.mu.Unlock()
}

func keyTouchSwap() map[int64]struct{} {
	keyTouches.mu.Lock()
	defer keyTouches.mu.Unlock()
	m := keyTouches.m
	keyTouches.m = map[int64]struct{}{}
	return m
}

// ------------------------------------------------------------- 路由

func registerGateway(mux *http.ServeMux) {
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"ok": true, "service": cfg.AppName}, nil)
	})
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{
			"service": cfg.AppName,
			"endpoints": map[string]any{
				"admin":     "/admin",
				"health":    "/healthz",
				"providers": []string{"bilibili", "doubao"},
			},
		}, nil)
	})

	mux.HandleFunc("POST /doubao/translate", func(w http.ResponseWriter, r *http.Request) {
		deeplxHandler(w, r, "")
	})
	mux.HandleFunc("POST /doubao", func(w http.ResponseWriter, r *http.Request) {
		deeplxHandler(w, r, "")
	})
	mux.HandleFunc("POST /doubao/{api_key}/translate", func(w http.ResponseWriter, r *http.Request) {
		deeplxHandler(w, r, r.PathValue("api_key"))
	})
	mux.HandleFunc("POST /doubao/{api_key}", func(w http.ResponseWriter, r *http.Request) {
		deeplxHandler(w, r, r.PathValue("api_key"))
	})
	for _, p := range []string{"OPTIONS /doubao/translate", "OPTIONS /doubao",
		"OPTIONS /doubao/{api_key}/translate", "OPTIONS /doubao/{api_key}"} {
		mux.HandleFunc(p, func(w http.ResponseWriter, r *http.Request) {
			applyCORS(w, nil)
			w.WriteHeader(http.StatusNoContent)
		})
	}

	mux.HandleFunc("/bilibili/{path...}", bilibiliHandler)
	mux.HandleFunc("OPTIONS /bilibili/{path...}", func(w http.ResponseWriter, r *http.Request) {
		applyCORS(w, nil)
		w.WriteHeader(http.StatusNoContent)
	})
}

// ------------------------------------------------------------- 豆包入口排队

var (
	doubaoGlobalSem chan struct{}
	doubaoSemOnce   sync.Once
	doubaoKeySems   sync.Map // keyName -> chan struct{}
)

func globalSem() chan struct{} {
	doubaoSemOnce.Do(func() {
		doubaoGlobalSem = make(chan struct{}, cfg.DoubaoQueueCap)
	})
	return doubaoGlobalSem
}

func keySemaphore(name string) chan struct{} {
	if v, ok := doubaoKeySems.Load(name); ok {
		return v.(chan struct{})
	}
	v, _ := doubaoKeySems.LoadOrStore(name, make(chan struct{}, cfg.DoubaoKeyCap))
	return v.(chan struct{})
}

// acquireDoubaoSlot 全局 + 单 Key 双上限排队；等待超限返回 denied=true。
func acquireDoubaoSlot(key string) (release func(), denied bool) {
	wait := time.Duration(cfg.DoubaoQueueWait) * time.Second

	g := globalSem()
	t1 := time.NewTimer(wait)
	select {
	case g <- struct{}{}:
		t1.Stop()
	case <-t1.C:
		return nil, true
	}

	k := keySemaphore(key)
	t2 := time.NewTimer(wait)
	select {
	case k <- struct{}{}:
		t2.Stop()
		return func() {
			<-k
			<-g
		}, false
	case <-t2.C:
		<-g
		return nil, true
	}
}

// ------------------------------------------------------------- DeepLX 处理

type deeplxRequest struct {
	Text       string `json:"text"`
	SourceLang string `json:"source_lang"`
	TargetLang string `json:"target_lang"`
}

func deeplxHandler(w http.ResponseWriter, r *http.Request, pathKey string) {
	start := time.Now()
	ip := clientIP(r)

	key, deny := checkKey(r, pathKey)
	if deny != nil {
		applyCORS(w, nil)
		writeJSON(w, deny.status, deny.body, nil)
		return
	}
	keyName := ""
	if key != nil {
		keyName = key.Name
	}

	raw, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
	if err != nil {
		store.LogUsage("doubao", "deeplx", false, strPtr("400"), nil, nil, ip, "read body")
		applyCORS(w, nil)
		writeJSON(w, 400, map[string]any{"code": 400, "message": "请求体不是合法 JSON"}, nil)
		return
	}
	var req deeplxRequest
	if err := json.Unmarshal(raw, &req); err != nil || req.Text == "" {
		store.LogUsage("doubao", "deeplx", false, strPtr("400"), nil, nil, ip, "no text")
		applyCORS(w, nil)
		writeJSON(w, 400, map[string]any{"code": 400, "message": "缺少 text 字段"}, nil)
		return
	}

	release, denied := acquireDoubaoSlot(keyName)
	if denied {
		store.LogUsage("doubao", "deeplx", false, strPtr("429"), nil, nil, ip, "queue wait timeout")
		applyCORS(w, nil)
		writeJSON(w, 429, map[string]any{"code": 429, "message": "排队超时，请稍后重试"},
			map[string]string{"Retry-After": "2"})
		return
	}
	defer release()

	data, perr := doubaoSvc.Translate(r.Context(), keyName, req.Text, req.SourceLang, req.TargetLang)

	applyCORS(w, nil)
	if perr != nil {
		var code *string
		if perr.Code != 0 {
			code = strPtr(fmt.Sprint(perr.Code))
		}
		store.LogUsage("doubao", "deeplx", false, code, nil,
			int64Ptr(int(time.Since(start).Milliseconds())), ip, perr.Msg)
		writeJSON(w, perr.HTTPStatus, map[string]any{
			"code": perr.Code, "message": perr.Msg}, nil)
		return
	}
	store.LogUsage("doubao", "deeplx", true, nil, int64Ptr(len(req.Text)),
		int64Ptr(int(time.Since(start).Milliseconds())), ip, "")
	writeJSON(w, 200, map[string]any{
		"code":        200,
		"id":          time.Now().UnixMilli(),
		"data":        data,
		"method":      "transhub-doubao",
		"source_lang": orDefault(req.SourceLang, "auto"),
		"target_lang": req.TargetLang,
	}, nil)
}

// ------------------------------------------------------------- B站透传

func bilibiliHandler(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	ip := clientIP(r)
	path := r.PathValue("path")

	_, deny := checkKey(r, "")
	if deny != nil {
		applyCORS(w, nil)
		writeJSON(w, deny.status, deny.body, nil)
		return
	}

	if !biliSvc.entryAllow() {
		store.LogUsage("bilibili", path, false, strPtr("429"), nil, nil, ip, "rate limited")
		applyCORS(w, nil)
		writeJSON(w, 429, map[string]any{"code": 429, "message": "请求过于频繁，已被限流"},
			map[string]string{"Retry-After": "1"})
		return
	}

	var bodyLen int64
	if r.ContentLength > 0 {
		bodyLen = r.ContentLength
	}
	var outcome ProxyOutcome
	if path == "v1/chat/completions" || path == "chat/completions" {
		// OpenAI chat 走适配层：上游 /v1 已故障，改接网页端 translate_stream
		outcome = biliSvc.ProxyChat(w, r)
		if outcome.Bytes > 0 {
			bodyLen = outcome.Bytes
		}
	} else {
		outcome = biliSvc.Proxy(w, r, path)
	}
	okv := outcome.Status < 400
	msg := outcome.Err
	if !okv && msg != "" {
		msg = clip(msg, 200)
	}
	store.LogUsage("bilibili", path, okv, strPtr(fmt.Sprint(outcome.Status)), &bodyLen,
		int64Ptr(int(time.Since(start).Milliseconds())), ip, msg)
}

// ------------------------------------------------------------- 小工具

func strPtr(s string) *string { return &s }

func int64Ptr(v int) *int64 {
	n := int64(v)
	return &n
}

func orDefault(s, def string) string {
	if s == "" {
		return def
	}
	return s
}
