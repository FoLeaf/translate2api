package main

// 后台管理：密码登录 + 服务商状态/登录/凭据 + API Key + 用量 + 设置。
// 接口路径与 Python 版保持一致，会话 Cookie 同名同格式（迁移后无缝续用）。

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

//go:embed web/admin.html
var adminHTML []byte

const adminCookie = "th_session"

func registerAdmin(mux *http.ServeMux) {
	mux.HandleFunc("GET /admin", adminPage)
	mux.HandleFunc("GET /admin/login", adminPage)
	mux.HandleFunc("POST /admin/api/login", adminAPILogin)
	mux.HandleFunc("POST /admin/api/logout", adminAPILogout)
	mux.HandleFunc("GET /admin/api/overview", guard(adminAPIOverview))
	mux.HandleFunc("GET /admin/api/providers/{pid}", guard(adminAPIProvider))
	mux.HandleFunc("PUT /admin/api/providers/{pid}/config", guardCSRF(adminAPIProviderConfig))
	mux.HandleFunc("POST /admin/api/providers/{pid}/test", guardCSRF(adminAPIProviderTest))
	mux.HandleFunc("POST /admin/api/providers/{pid}/login/start", guardCSRF(adminAPILoginStart))
	mux.HandleFunc("GET /admin/api/providers/{pid}/login/poll", guard(adminAPILoginPoll))
	mux.HandleFunc("GET /admin/api/providers/{pid}/login/frame", guard(adminAPILoginFrame))
	mux.HandleFunc("POST /admin/api/providers/{pid}/login/cancel", guardCSRF(adminAPILoginCancel))
	mux.HandleFunc("POST /admin/api/providers/{pid}/credentials/manual", guardCSRF(adminAPICredManual))
	mux.HandleFunc("DELETE /admin/api/providers/{pid}/credentials/{name}", guardCSRF(adminAPICredDelete))
	mux.HandleFunc("GET /admin/api/keys", guard(adminAPIKeysList))
	mux.HandleFunc("POST /admin/api/keys", guardCSRF(adminAPIKeysCreate))
	mux.HandleFunc("POST /admin/api/keys/{key_id}/toggle", guardCSRF(adminAPIKeysToggle))
	mux.HandleFunc("DELETE /admin/api/keys/{key_id}", guardCSRF(adminAPIKeysDelete))
	mux.HandleFunc("GET /admin/api/usage", guard(adminAPIUsage))
	mux.HandleFunc("POST /admin/api/password", guardCSRF(adminAPIPassword))
	mux.HandleFunc("GET /admin/api/rate", guard(adminAPIRateGet))
	mux.HandleFunc("PUT /admin/api/rate", guardCSRF(adminAPIRatePut))
}

func adminPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(adminHTML)
}

func isAuthed(r *http.Request) bool {
	c, err := r.Cookie(adminCookie)
	if err != nil {
		return false
	}
	return checkSession(secret, c.Value)
}

func guard(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !isAuthed(r) {
			writeJSON(w, 401, map[string]any{"ok": false, "detail": "未登录或会话过期"}, nil)
			return
		}
		h(w, r)
	}
}

// guardCSRF 状态变更接口额外要求 X-Requested-With（与 Python 版一致）。
func guardCSRF(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !isAuthed(r) {
			writeJSON(w, 401, map[string]any{"ok": false, "detail": "未登录或会话过期"}, nil)
			return
		}
		if r.Header.Get("X-Requested-With") != "fetch" {
			writeJSON(w, 403, map[string]any{"ok": false, "detail": "缺少 CSRF 头"}, nil)
			return
		}
		h(w, r)
	}
}

func readBodyJSON(r *http.Request) map[string]any {
	var m map[string]any
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	_ = json.Unmarshal(raw, &m)
	if m == nil {
		m = map[string]any{}
	}
	return m
}

// ------------------------------------------------------------- 登录

func adminAPILogin(w http.ResponseWriter, r *http.Request) {
	ip := clientIP(r)
	if loginAttemptsExceeded(ip) {
		writeJSON(w, 429, map[string]any{"ok": false, "detail": "失败次数过多，请 15 分钟后再试"}, nil)
		return
	}
	body := readBodyJSON(r)
	password, _ := body["password"].(string)
	stored, _ := store.GetSetting("admin_password_hash")
	var storedStr string
	_ = json.Unmarshal([]byte(stored), &storedStr)
	if storedStr == "" {
		storedStr = strings.Trim(stored, `"`)
	}
	if storedStr == "" || !VerifyPassword(password, storedStr) {
		recordLoginFail(ip)
		writeJSON(w, 401, map[string]any{"ok": false, "detail": "密码错误"}, nil)
		return
	}
	recordLoginOK(ip)
	http.SetCookie(w, &http.Cookie{
		Name:     adminCookie,
		Value:    issueSession(secret, cfg.SessionDays),
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   r.Header.Get("X-Forwarded-Proto") == "https",
		MaxAge:   cfg.SessionDays * 86400,
	})
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

func adminAPILogout(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: adminCookie, Value: "", Path: "/", MaxAge: -1})
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

// ------------------------------------------------------------- 概览

func providerStatus(pid string) map[string]any {
	if pid == "doubao" {
		cred, err := store.GetCredential("doubao")
		if err != nil || cred == nil {
			return map[string]any{"ok": false,
				"detail": "未配置 Cookie：请到「服务商」页扫码登录豆包", "credential": nil}
		}
		captured := cred.UpdatedAt
		if v, ok := cred.Data["captured_at"].(float64); ok {
			captured = int64(v)
		}
		ageDays := float64(time.Now().Unix()-captured) / 86400
		ok := cred.Status == "active"
		detail := "Cookie 正常"
		if !ok {
			detail = "Cookie 已失效（上游返回登录过期），请重新扫码登录"
		}
		account, _ := cred.Data["account"].(string)
		return map[string]any{"ok": ok,
			"detail": fmt.Sprintf("%s，更新于 %.1f 天前", detail, ageDays),
			"credential": map[string]any{"name": cred.Name, "status": cred.Status,
				"updated_at": cred.UpdatedAt, "account": account}}
	}
	cred, _ := store.GetCredential("bilibili")
	var credInfo any
	if cred != nil {
		account, _ := cred.Data["account"].(string)
		credInfo = map[string]any{"name": cred.Name, "status": cred.Status,
			"updated_at": cred.UpdatedAt, "account": account}
	}
	return map[string]any{"ok": true, "detail": "免费 API 无需登录即可用", "credential": credInfo}
}

func adminAPIOverview(w http.ResponseWriter, r *http.Request) {
	proto_ := r.Header.Get("X-Forwarded-Proto")
	if proto_ == "" {
		proto_ = "http"
	}
	host := r.Host
	base := proto_ + "://" + host
	providers := []map[string]any{
		{"id": "doubao", "name": "豆包翻译", "desc": "豆包网页翻译接口，需豆包账号 Cookie",
			"protocols": []string{"deeplx"}, "login_supported": true, "status": providerStatus("doubao")},
		{"id": "bilibili", "name": "B站 Index-Translate", "desc": "B站免费开放 API（OpenAI 兼容，无需 Key）",
			"protocols": []string{"openai"}, "login_supported": true, "status": providerStatus("bilibili")},
	}
	stats, _ := store.UsageStats(86400)
	keys, _ := store.ListAPIKeys()
	writeJSON(w, 200, map[string]any{
		"ok": true, "providers": providers, "stats": stats,
		"key_required": store.KeyEnabledExists(), "key_count": len(keys),
		"base_urls": map[string]string{
			"doubao_deeplx":  base + "/doubao/{{apiKey}}/translate",
			"bilibili_openai": base + "/bilibili/v1",
		},
		"gateway": map[string]any{
			"runtime":   "go",
			"queue_wait": cfg.DoubaoQueueWait, "queue_cap": cfg.DoubaoQueueCap,
			"key_cap":   cfg.DoubaoKeyCap, "doubao_timeout": cfg.DoubaoTimeout,
			"batch_max": cfg.DoubaoBatchMax, "bili_up_conc": cfg.BiliUpConc,
		},
	}, nil)
}

// ------------------------------------------------------------- 服务商

func adminAPIProvider(w http.ResponseWriter, r *http.Request) {
	pid := r.PathValue("pid")
	if pid != "doubao" && pid != "bilibili" {
		writeJSON(w, 404, map[string]any{"ok": false, "detail": "未知服务商"}, nil)
		return
	}
	cfgv := map[string]any{}
	loginMode := any(nil)
	if pid == "doubao" {
		engine := "1"
		scene := 1
		var e string
		var s int
		if store.GetSettingJSON("doubao_engine", &e) && e != "" {
			engine = e
		}
		if store.GetSettingJSON("doubao_scene", &s) && s != 0 {
			scene = s
		}
		cfgv = map[string]any{"engine": engine, "scene": scene}
		loginMode = "frame"
	} else {
		loginMode = "qr_png"
	}
	creds, _ := store.ListCredentials(pid)
	var credList []map[string]any
	for _, c := range creds {
		account, _ := c.Data["account"].(string)
		uid := ""
		if v, ok := c.Data["uid"].(string); ok {
			uid = v
		} else if v, ok := c.Data["uid"].(float64); ok {
			uid = strconv.FormatInt(int64(v), 10)
		}
		credList = append(credList, map[string]any{
			"name": c.Name, "status": c.Status, "updated_at": c.UpdatedAt,
			"account": account, "uid": uid})
	}
	if credList == nil {
		credList = []map[string]any{}
	}
	writeJSON(w, 200, map[string]any{"ok": true, "id": pid,
		"status": providerStatus(pid), "config": cfgv, "login_mode": loginMode,
		"credentials": credList}, nil)
}

func adminAPIProviderConfig(w http.ResponseWriter, r *http.Request) {
	pid := r.PathValue("pid")
	if pid != "doubao" {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "该服务商无可配置项"}, nil)
		return
	}
	body := readBodyJSON(r)
	engine, _ := body["engine"].(string)
	if engine == "" {
		engine = "1"
	}
	if engine != "0" && engine != "1" && engine != "3" {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "engine 只能是 0/1/3"}, nil)
		return
	}
	scene := 1
	switch v := body["scene"].(type) {
	case float64:
		scene = int(v)
	case string:
		scene, _ = strconv.Atoi(v)
	}
	if scene != 1 && scene != 2 && scene != 3 && scene != 6 {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "scene 只能是 1/2/3/6"}, nil)
		return
	}
	_ = store.SetSetting("doubao_engine", engine)
	_ = store.SetSetting("doubao_scene", scene)
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

func adminAPIProviderTest(w http.ResponseWriter, r *http.Request) {
	pid := r.PathValue("pid")
	if pid == "doubao" {
		t0 := time.Now()
		res, perr := doubaoSvc.Translate(context.Background(), "admin-test", "Hello world", "EN", "ZH")
		if perr != nil {
			writeJSON(w, 200, map[string]any{"ok": false, "detail": perr.Msg}, nil)
			return
		}
		out := res
		if len(out) > 60 {
			out = out[:60]
		}
		writeJSON(w, 200, map[string]any{"ok": true,
			"detail": fmt.Sprintf("翻译成功（%.1fs）: %s", time.Since(t0).Seconds(), out)}, nil)
		return
	}
	if pid == "bilibili" {
		t0 := time.Now()
		req, _ := http.NewRequest(http.MethodGet, cfg.BilibiliUpstream+"/v1/models", nil)
		resp, err := biliSvc.hc.Do(req)
		if err != nil {
			writeJSON(w, 200, map[string]any{"ok": false, "detail": err.Error()}, nil)
			return
		}
		defer resp.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		writeJSON(w, 200, map[string]any{"ok": strings.Contains(string(raw), "Index-Translate"),
			"detail": fmt.Sprintf("上游返回 %d（%.1fs）: %s", resp.StatusCode,
				time.Since(t0).Seconds(), clip(string(raw), 80))}, nil)
		return
	}
	writeJSON(w, 404, map[string]any{"ok": false, "detail": "未知服务商"}, nil)
}

func adminAPILoginStart(w http.ResponseWriter, r *http.Request) {
	switch r.PathValue("pid") {
	case "doubao":
		writeJSON(w, 200, doubaoFlow.Start(), nil)
	case "bilibili":
		writeJSON(w, 200, biliFlow.Start(), nil)
	default:
		writeJSON(w, 404, map[string]any{"ok": false, "detail": "该服务商不支持登录"}, nil)
	}
}

func adminAPILoginPoll(w http.ResponseWriter, r *http.Request) {
	var res map[string]any
	switch r.PathValue("pid") {
	case "doubao":
		res = doubaoFlow.Poll()
	case "bilibili":
		res = biliFlow.Poll()
	default:
		writeJSON(w, 404, map[string]any{"ok": false, "detail": "该服务商不支持登录"}, nil)
		return
	}
	if res == nil {
		res = map[string]any{}
	}
	res["ok"] = true
	writeJSON(w, 200, res, nil)
}

func adminAPILoginFrame(w http.ResponseWriter, r *http.Request) {
	if r.PathValue("pid") != "doubao" {
		writeJSON(w, 404, map[string]any{"ok": false}, nil)
		return
	}
	png, _ := doubaoFlow.Frame()
	if len(png) == 0 {
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(png)
}

func adminAPILoginCancel(w http.ResponseWriter, r *http.Request) {
	switch r.PathValue("pid") {
	case "doubao":
		doubaoFlow.Cancel()
	case "bilibili":
		biliFlow.Cancel()
	}
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

func adminAPICredManual(w http.ResponseWriter, r *http.Request) {
	pid := r.PathValue("pid")
	body := readBodyJSON(r)
	cookie, _ := body["cookie"].(string)
	cookie = strings.TrimSpace(cookie)
	if len(cookie) < 20 {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "Cookie 太短"}, nil)
		return
	}
	_ = store.PutCredential(pid, map[string]any{
		"cookie": cookie, "manual": true, "captured_at": time.Now().Unix(),
	}, "default", "active")
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

func adminAPICredDelete(w http.ResponseWriter, r *http.Request) {
	ok, _ := store.DeleteCredential(r.PathValue("pid"), r.PathValue("name"))
	writeJSON(w, 200, map[string]any{"ok": ok}, nil)
}

// ------------------------------------------------------------- API Key

func adminAPIKeysList(w http.ResponseWriter, r *http.Request) {
	keys, _ := store.ListAPIKeys()
	if keys == nil {
		keys = []APIKeyRow{}
	}
	writeJSON(w, 200, map[string]any{"ok": true, "keys": keys,
		"key_required": store.KeyEnabledExists()}, nil)
}

func adminAPIKeysCreate(w http.ResponseWriter, r *http.Request) {
	body := readBodyJSON(r)
	name, _ := body["name"].(string)
	name = strings.TrimSpace(name)
	if len(name) > 30 {
		name = name[:30]
	}
	if name == "" {
		name = "默认"
	}
	id, full, err := store.CreateAPIKey(name)
	if err != nil {
		writeJSON(w, 500, map[string]any{"ok": false, "detail": err.Error()}, nil)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "id": id, "key": full,
		"detail": "请立即复制保存，明文不再二次展示"}, nil)
}

func adminAPIKeysToggle(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("key_id"), 10, 64)
	if err != nil {
		writeJSON(w, 404, map[string]any{"ok": false, "detail": "不存在"}, nil)
		return
	}
	keys, _ := store.ListAPIKeys()
	found := false
	enabled := false
	for _, k := range keys {
		if k.ID == id {
			found, enabled = true, k.Enabled
			break
		}
	}
	if !found {
		writeJSON(w, 404, map[string]any{"ok": false, "detail": "不存在"}, nil)
		return
	}
	_ = store.SetAPIKeyEnabled(id, !enabled)
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

func adminAPIKeysDelete(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("key_id"), 10, 64)
	if err == nil {
		_ = store.DeleteAPIKey(id)
	}
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

// ------------------------------------------------------------- 用量/设置

func adminAPIUsage(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil {
		if v < 1 {
			v = 1
		}
		if v > 500 {
			v = 500
		}
		limit = v
	}
	rows, _ := store.RecentUsage(limit)
	if rows == nil {
		rows = []map[string]any{}
	}
	writeJSON(w, 200, map[string]any{"ok": true, "rows": rows}, nil)
}

func adminAPIPassword(w http.ResponseWriter, r *http.Request) {
	body := readBodyJSON(r)
	old, _ := body["old"].(string)
	nw, _ := body["new"].(string)
	stored, _ := store.GetSetting("admin_password_hash")
	var storedStr string
	_ = json.Unmarshal([]byte(stored), &storedStr)
	if storedStr == "" {
		storedStr = strings.Trim(stored, `"`)
	}
	if storedStr == "" || !VerifyPassword(old, storedStr) {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "旧密码错误"}, nil)
		return
	}
	if len(nw) < 8 {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "新密码至少 8 位"}, nil)
		return
	}
	_ = store.SetSetting("admin_password_hash", HashPassword(nw))
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}

func adminAPIRateGet(w http.ResponseWriter, r *http.Request) {
	rate, burst := 8.0, 16.0
	var cfgv []float64
	if store.GetSettingJSON("rate_bilibili", &cfgv) && len(cfgv) == 2 {
		rate, burst = cfgv[0], cfgv[1]
	}
	writeJSON(w, 200, map[string]any{"ok": true, "rates": []map[string]any{
		{"provider": "bilibili", "rate": rate, "burst": burst},
		{"provider": "doubao", "rate": 0, "burst": 0,
			"note": "豆包线路为入口排队（等待上限/全局/单Key），不使用令牌桶，参数经环境变量调整"},
	}}, nil)
}

func adminAPIRatePut(w http.ResponseWriter, r *http.Request) {
	body := readBodyJSON(r)
	provider, _ := body["provider"].(string)
	if provider != "bilibili" {
		writeJSON(w, 404, map[string]any{"ok": false, "detail": "未知服务商（仅 B站线路使用令牌桶）"}, nil)
		return
	}
	rate, ok1 := body["rate"].(float64)
	burst, ok2 := body["burst"].(float64)
	if !ok1 || !ok2 || rate < 0.1 || rate > 50 || burst < 1 || burst > 100 {
		writeJSON(w, 400, map[string]any{"ok": false, "detail": "参数非法（rate 0.1-50，burst 1-100）"}, nil)
		return
	}
	_ = store.SetSetting("rate_bilibili", []float64{rate, burst})
	writeJSON(w, 200, map[string]any{"ok": true}, nil)
}
