package main

// B站 Index-Translate 反向代理：OpenAI 兼容透传。
// - B站网关见非自家域名 Origin 即 412，服务端剥头规避
// - 常驻连接池 + 上游并发信号量（默认 8）
// - 真流式透传（逐块 Flush），用量统计含流式传输耗时与字节数
// - 网关鉴权头绝不外发（剥离 authorization 等）

import (
	"bytes"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

var biliStripReq = map[string]bool{
	"origin": true, "referer": true, "host": true, "cookie": true,
	"content-length": true, "connection": true, "keep-alive": true,
	"transfer-encoding": true, "te": true, "upgrade": true,
	"proxy-authorization": true, "proxy-connection": true,
	"accept-encoding": true, "x-forwarded-for": true, "x-forwarded-host": true,
	"x-forwarded-proto": true, "x-real-ip": true, "true-client-ip": true,
	"cdn-loop": true,
	// 网关鉴权头绝不外发
	"authorization": true, "deepl-auth-key": true, "x-api-key": true, "api-key": true,
}

var biliStripResp = map[string]bool{
	"content-length": true, "transfer-encoding": true, "connection": true,
	"keep-alive": true, "alt-svc": true, "server": true, "date": true,
}

const biliDefaultUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"

type BiliService struct {
	hc  *http.Client
	sem chan struct{}

	bucketMu   sync.Mutex
	bucket     *tokenBucket
	bucketRate float64
	bucketCap  float64

	// 可选 Cookie 注入（当前上游不需要，留着以备官方收紧），60 秒缓存
	credMu sync.Mutex
	credAt time.Time
	credCo string
}

var biliSvc *BiliService

func newBiliService() *BiliService {
	s := &BiliService{
		hc: &http.Client{
			Transport: &http.Transport{
				DialContext: (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
				// 空闲连接池须覆盖信号量（32）并留余量，否则高并发下部分
				// 请求每轮重建 TCP+TLS，把网关自身延迟混进上游耗时
				MaxIdleConns:        48,
				MaxIdleConnsPerHost: 48,
				IdleConnTimeout:     120 * time.Second,
				TLSHandshakeTimeout: 10 * time.Second,
				// 上游偶发停滞可达 60 秒级，网关不先于上游掐断，上限交给 B站
				ResponseHeaderTimeout: 60 * time.Second,
			},
		},
		sem: make(chan struct{}, cfg.BiliUpConc),
	}
	biliSvc = s
	return s
}

// entryAllow 入口令牌桶（ReadFrog 客户端默认 8/秒、突发 20，网关兜底，后台可调）。
func (s *BiliService) entryAllow() bool {
	rate, burst := 8.0, 16.0
	var cfgv []float64
	if store.GetSettingJSON("rate_bilibili", &cfgv) && len(cfgv) == 2 {
		rate, burst = cfgv[0], cfgv[1]
	}
	s.bucketMu.Lock()
	defer s.bucketMu.Unlock()
	if s.bucket == nil || s.bucketRate != rate || s.bucketCap != burst {
		s.bucket = &tokenBucket{rate: rate, capacity: burst, tokens: burst, last: time.Now()}
		s.bucketRate, s.bucketCap = rate, burst
	}
	return s.bucket.acquire()
}

func (s *BiliService) cookie() string {
	s.credMu.Lock()
	defer s.credMu.Unlock()
	if s.credAt.IsZero() || time.Since(s.credAt) > 60*time.Second {
		s.credCo = ""
		if cred, err := store.GetCredential("bilibili"); err == nil && cred != nil {
			if c, _ := cred.Data["cookie"].(string); c != "" && cred.Status == "active" {
				s.credCo = c
			}
		}
		s.credAt = time.Now()
	}
	return s.credCo
}

// ProxyOutcome 透传结果观测（写用量日志用）。
type ProxyOutcome struct {
	Status int
	Bytes  int64
	Err    string
}

// Proxy 执行一次透传；流式响应结束后才返回，ms 与字节数含流式传输。
func (s *BiliService) Proxy(w http.ResponseWriter, r *http.Request, path string) ProxyOutcome {
	url := cfg.BilibiliUpstream + "/" + strings.TrimPrefix(path, "/")
	if r.URL.RawQuery != "" {
		url += "?" + r.URL.RawQuery
	}

	// 上游并发信号量：等待上限 15 秒，超限回 429
	timer := time.NewTimer(15 * time.Second)
	select {
	case s.sem <- struct{}{}:
		timer.Stop()
	case <-timer.C:
		writeJSON(w, 429, map[string]any{"error": map[string]any{
			"message": "上游并发繁忙，请稍后重试", "type": "proxy_error"}},
			map[string]string{"Retry-After": "1"})
		return ProxyOutcome{Status: 429}
	}
	defer func() { <-s.sem }()

	body, err := io.ReadAll(io.LimitReader(r.Body, 32<<20))
	if err != nil {
		writeJSON(w, 400, map[string]any{"error": map[string]any{
			"message": "读取请求体失败", "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 400, Err: "read body: " + err.Error()}
	}

	req, err := http.NewRequest(r.Method, url, bytes.NewReader(body))
	if err != nil {
		writeJSON(w, 502, map[string]any{"error": map[string]any{
			"message": "构造上游请求失败", "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 502, Err: err.Error()}
	}
	for k, vs := range r.Header {
		if biliStripReq[strings.ToLower(k)] {
			continue
		}
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	// 上游 WAF 会按 UA 判机器人（实测 Python-urllib UA 直连必 412），
	// 客户端 UA 一律不透传，统一用网关自己的浏览器 UA
	req.Header.Set("User-Agent", biliDefaultUA)
	req.Header.Set("Accept-Encoding", "identity")
	if c := s.cookie(); c != "" {
		req.Header.Set("Cookie", c)
	}

	resp, err := s.hc.Do(req)
	if err != nil {
		writeJSON(w, 502, map[string]any{"error": map[string]any{
			"message": "upstream network error: " + err.Error(), "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 502, Err: err.Error()}
	}
	defer resp.Body.Close()

	outHeader := w.Header()
	for k, vs := range resp.Header {
		if biliStripResp[strings.ToLower(k)] {
			continue
		}
		for _, v := range vs {
			outHeader.Add(k, v)
		}
	}
	for k, v := range corsHeaders(nil) {
		outHeader.Set(k, v)
	}

	w.WriteHeader(resp.StatusCode)

	var total int64
	buf := make([]byte, 32*1024)
	flusher, _ := w.(http.Flusher)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return ProxyOutcome{Status: resp.StatusCode, Bytes: total, Err: "client write: " + werr.Error()}
			}
			total += int64(n)
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return ProxyOutcome{Status: resp.StatusCode, Bytes: total, Err: "upstream read: " + rerr.Error()}
		}
	}
	return ProxyOutcome{Status: resp.StatusCode, Bytes: total}
}
