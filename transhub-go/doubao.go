package main

// 豆包翻译服务：上游 stream_article_translate。
// 设计要点：
// - 上游串行调用（共享 Cookie，防风控），凑批摊薄成本：一次带多段与单段耗时几乎相同
// - 单次调用总超时 45 秒（上游间歇停滞实测 121-189 秒，尽早失败释放锁）
// - 上游错误不做重试与加工，单次调用直接把结果返回客户端（重试交给 ReadFrog 客户端）
// - 凑批按 Key 轮询取段，单 Key 整页翻译不会占满批次饿死其他 Key

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	doubaoUA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
	doubaoStreamPath = "/samantha/plugin/stream_article_translate"
	doubaoMaxChars   = 9000
	batchWindow      = 300 * time.Millisecond
	batchMaxChars    = 12000
	credCacheTTL     = 30 * time.Second
)

// ProviderError 服务商错误：code 为上游错误码，httpStatus 为返回给客户端的状态码。
type ProviderError struct {
	Msg              string
	Code             int
	HTTPStatus       int
	CredentialExpired bool
}

func (e *ProviderError) Error() string { return e.Msg }

func providerErr(msg string, code, httpStatus int) *ProviderError {
	return &ProviderError{Msg: msg, Code: code, HTTPStatus: httpStatus}
}

var doubaoLangMap = map[string]string{
	"ZH": "zh", "ZH-HANS": "zh", "ZH-HANT": "zh-Hant", "ZH-TW": "zh-Hant",
	"EN": "en", "JA": "ja", "KO": "ko", "DE": "de", "FR": "fr", "ES": "es",
	"PT": "pt", "RU": "ru", "IT": "it", "AR": "ar", "ID": "id", "VI": "vi",
	"TH": "th", "MS": "ms", "TL": "fil", "FIL": "fil", "UZ": "uz",
}

var doubaoKnownLow = map[string]bool{
	"en": true, "ar": true, "de": true, "es": true, "es-es": true, "fil": true,
	"fr": true, "id": true, "it": true, "ja": true, "ko": true, "ms": true,
	"pt": true, "ru": true, "th": true, "uz": true, "vi": true, "zh": true,
	"zh-hant": true,
}

func toDoubaoLang(code string) string {
	raw := strings.TrimSpace(code)
	if raw == "" {
		return ""
	}
	up := strings.ToUpper(raw)
	if v, ok := doubaoLangMap[up]; ok {
		return v
	}
	low := strings.ToLower(raw)
	switch low {
	case "zh-cn", "zh-hans", "zh-sg":
		return "zh"
	case "zh-tw", "zh-hk", "zh-hant", "zh-mo":
		return "zh-Hant"
	}
	if doubaoKnownLow[low] {
		switch low {
		case "zh-hant":
			return "zh-Hant"
		case "es-es":
			return "es-ES"
		}
		return low
	}
	return ""
}

// ------------------------------------------------------------- SSE 解析

type sseEvent struct {
	name string
	data string
}

func parseSSEEvents(raw string) []sseEvent {
	var events []sseEvent
	var name string
	var dataLines []string
	flush := func() {
		if len(dataLines) > 0 {
			events = append(events, sseEvent{name: name, data: strings.Join(dataLines, "\n")})
		}
		name = ""
		dataLines = nil
	}
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimRight(line, "\r")
		if line == "" {
			flush()
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		field, value, _ := strings.Cut(line, ":")
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			name = value
		case "data":
			dataLines = append(dataLines, value)
		}
	}
	flush()
	return events
}

// readDoubaoStream 返回 {index: 译文}；JSON 错误体与 event:err 转为 ProviderError。
func readDoubaoStream(raw []byte, contentType string) (map[int]string, *ProviderError) {
	trimmed := bytes.TrimLeft(raw, " \t\r\n")
	isJSON := strings.Contains(strings.ToLower(contentType), "application/json") ||
		(len(trimmed) > 0 && trimmed[0] == '{')
	if isJSON {
		var frame map[string]any
		if err := json.Unmarshal(raw, &frame); err != nil {
			return nil, providerErr("响应既不是 SSE 也不是 JSON: "+clip(string(raw), 120), 0, 502)
		}
		if code, ok := toInt(frame["code"]); ok && code != 0 {
			expired := code == 710012001
			msg := "未知错误"
			if v, ok := frame["msg"].(string); ok && v != "" {
				msg = v
			} else if v, ok := frame["message"].(string); ok && v != "" {
				msg = v
			}
			status := 502
			if expired {
				status = 401
			}
			return nil, &ProviderError{Msg: msg, Code: code, HTTPStatus: status, CredentialExpired: expired}
		}
		return nil, providerErr("返回了非流式 JSON: "+clip(string(raw), 200), 0, 502)
	}

	items := map[int]string{}
	sawDone := false
	for _, ev := range parseSSEEvents(string(raw)) {
		switch ev.name {
		case "done":
			sawDone = true
		case "err":
			var f map[string]any
			_ = json.Unmarshal([]byte(ev.data), &f)
			msg := "流式错误帧"
			if v, ok := f["msg"].(string); ok && v != "" {
				msg = v
			}
			code := 710020702
			if c, ok := toInt(f["code"]); ok {
				code = c
			}
			return nil, providerErr(msg, code, 502)
		case "json":
			var frame map[string]any
			if err := json.Unmarshal([]byte(ev.data), &frame); err != nil {
				continue
			}
			if code, ok := toInt(frame["code"]); ok && code != 0 {
				expired := code == 710012001
				msg := "服务端错误"
				if v, ok := frame["msg"].(string); ok && v != "" {
					msg = v
				}
				status := 502
				if expired {
					status = 401
				}
				return nil, &ProviderError{Msg: msg, Code: code, HTTPStatus: status, CredentialExpired: expired}
			}
			fd, ok := frame["data"].(map[string]any)
			if !ok {
				continue
			}
			list, ok := fd["items"].([]any)
			if !ok {
				continue
			}
			for _, it := range list {
				m, ok := it.(map[string]any)
				if !ok {
					continue
				}
				idx, ok1 := toInt(m["index"])
				res, ok2 := m["res"].(string)
				if ok1 && ok2 {
					if old, exists := items[idx]; !exists || len(res) > len(old) {
						items[idx] = res
					}
				}
			}
		}
	}
	if len(items) == 0 {
		suffix := "（未收到 done）"
		if sawDone {
			suffix = "（收到 done）"
		}
		return nil, providerErr("翻译流结束但没有返回任何译文"+suffix, 0, 502)
	}
	return items, nil
}

func toInt(v any) (int, bool) {
	switch n := v.(type) {
	case float64:
		return int(n), true
	case int:
		return n, true
	case json.Number:
		i, err := n.Int64()
		return int(i), err == nil
	}
	return 0, false
}

// splitChunks 超长文本按行切块。
func splitChunks(text string) []string {
	if len(text) <= doubaoMaxChars {
		return []string{text}
	}
	var chunks []string
	var buf strings.Builder
	for _, line := range strings.Split(text, "\n") {
		if buf.Len()+len(line)+1 > doubaoMaxChars && buf.Len() > 0 {
			chunks = append(chunks, buf.String())
			buf.Reset()
			buf.WriteString(line)
		} else {
			if buf.Len() > 0 {
				buf.WriteByte('\n')
			}
			buf.WriteString(line)
		}
	}
	if buf.Len() > 0 {
		chunks = append(chunks, buf.String())
	}
	return chunks
}

// ------------------------------------------------------------- 上游服务

type DoubaoService struct {
	hc *http.Client
	// 上游串行锁：共享 Cookie + 防风控
	serial sync.Mutex
	// 凑批器
	batcher *batcher
	// 凭据缓存，避免热路径每请求查库
	credMu    sync.Mutex
	credAt    time.Time
	credCookie string
	credName  string
}

func newDoubaoService() *DoubaoService {
	s := &DoubaoService{
		hc: &http.Client{
			Timeout: time.Duration(cfg.DoubaoTimeout) * time.Second,
			Transport: &http.Transport{
				DialContext:         (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
				MaxIdleConns:        4,
				MaxIdleConnsPerHost: 4,
				IdleConnTimeout:     120 * time.Second,
				TLSHandshakeTimeout: 10 * time.Second,
			},
		},
	}
	s.batcher = newBatcher(s)
	doubaoSvc = s
	return s
}

var doubaoSvc *DoubaoService

// credential 取豆包 Cookie（带 30 秒缓存）。
func (s *DoubaoService) credential() (cookie string, perr *ProviderError) {
	s.credMu.Lock()
	defer s.credMu.Unlock()
	if s.credCookie != "" && time.Since(s.credAt) < credCacheTTL {
		return s.credCookie, nil
	}
	cred, err := store.GetCredential("doubao")
	if err != nil || cred == nil {
		return "", &ProviderError{Msg: "未配置豆包 Cookie：请到后台扫码登录或手动导入",
			Code: 710012001, HTTPStatus: 401, CredentialExpired: true}
	}
	c, _ := cred.Data["cookie"].(string)
	s.credCookie, s.credName, s.credAt = c, cred.Name, time.Now()
	return s.credCookie, nil
}

func (s *DoubaoService) invalidateCredential() {
	s.credMu.Lock()
	s.credCookie = ""
	s.credMu.Unlock()
	_ = store.SetCredentialStatus("doubao", "expired")
}

// callUpstream 串行调用上游一次带多段；网络类错误自动重试一次。
func (s *DoubaoService) callUpstream(texts []string, target string) (map[int]string, *ProviderError) {
	cookie, perr := s.credential()
	if perr != nil {
		return nil, perr
	}
	engine := "1"
	scene := 1
	var e2 string
	var s2 int
	if store.GetSettingJSON("doubao_engine", &e2) && e2 != "" {
		engine = e2
	}
	if store.GetSettingJSON("doubao_scene", &s2) && s2 != 0 {
		scene = s2
	}

	body, _ := json.Marshal(map[string]any{
		"raw_text":          texts,
		"target_lang":       target,
		"translate_service": engine,
		"scene":             scene,
		"frontend_source":   1,
	})

	s.serial.Lock()
	defer s.serial.Unlock()

	req, err := http.NewRequest(http.MethodPost, cfg.DoubaoUpstream+doubaoStreamPath, bytes.NewReader(body))
	if err != nil {
		return nil, providerErr("构造上游请求失败: "+err.Error(), -1, 500)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "*/*")
	req.Header.Set("User-Agent", doubaoUA)
	req.Header.Set("Referer", cfg.DoubaoUpstream+"/")
	req.Header.Set("Cookie", cookie)

	resp, err := s.hc.Do(req)
	if err != nil {
		return nil, providerErr("网络错误: "+err.Error(), -1, 504)
	}
	raw, readErr := io.ReadAll(resp.Body)
	resp.Body.Close()
	if readErr != nil {
		return nil, providerErr("读取上游响应失败: "+readErr.Error(), -1, 504)
	}
	items, perr := readDoubaoStream(raw, resp.Header.Get("Content-Type"))
	if perr != nil {
		if perr.CredentialExpired {
			s.invalidateCredential()
		}
		return nil, perr
	}
	return items, nil
}

// Translate 对外入口：普通段落走凑批，超长文本逐块直发。
func (s *DoubaoService) Translate(ctx context.Context, key, text, sourceLang, targetLang string) (string, *ProviderError) {
	target := toDoubaoLang(targetLang)
	if target == "" {
		return "", providerErr(fmt.Sprintf("不支持的目标语言: %q（豆包支持 19 种语言码）", targetLang), 400, 400)
	}
	chunks := splitChunks(text)
	if len(chunks) == 1 {
		return s.batcher.submit(ctx, key, chunks[0], target)
	}
	var out []string
	for _, chunk := range chunks {
		items, perr := s.callUpstream([]string{chunk}, target)
		if perr != nil {
			return "", perr
		}
		if v, ok := items[0]; ok {
			out = append(out, v)
		} else {
			return "", providerErr("该批没有 index=0 的译文，收到下标: "+indexList(items), 0, 502)
		}
	}
	return strings.Join(out, "\n"), nil
}

func indexList(items map[int]string) string {
	var keys []int
	for k := range items {
		keys = append(keys, k)
	}
	sort.Ints(keys)
	return fmt.Sprint(keys)
}

// ------------------------------------------------------------- 凑批器

type batchItem struct {
	key   string
	text  string
	ctx   context.Context
	ch    chan batchResult
}

type batchResult struct {
	text string
	perr *ProviderError
}

type targetQueues struct {
	order  []string // Key 名按首次出现顺序，取过即移到队尾（轮询公平）
	queues map[string][]*batchItem
}

type batcher struct {
	mu      sync.Mutex
	pending map[string]*targetQueues
	svc     *DoubaoService
}

func newBatcher(svc *DoubaoService) *batcher {
	return &batcher{pending: map[string]*targetQueues{}, svc: svc}
}

func (b *batcher) submit(ctx context.Context, key, text, target string) (string, *ProviderError) {
	item := &batchItem{key: key, text: text, ctx: ctx, ch: make(chan batchResult, 1)}
	b.mu.Lock()
	tq := b.pending[target]
	if tq == nil {
		tq = &targetQueues{queues: map[string][]*batchItem{}}
		b.pending[target] = tq
	}
	wasIdle := b.totalItems(tq) == 0
	tq.queues[key] = append(tq.queues[key], item)
	if !containsStr(tq.order, key) {
		tq.order = append(tq.order, key)
	}
	if wasIdle {
		time.AfterFunc(batchWindow, func() { b.flush(target) })
	}
	b.mu.Unlock()

	select {
	case r := <-item.ch:
		return r.text, r.perr
	case <-ctx.Done():
		return "", providerErr("客户端已取消", 499, 499)
	}
}

func (b *batcher) totalItems(tq *targetQueues) int {
	n := 0
	for _, q := range tq.queues {
		n += len(q)
	}
	return n
}

func containsStr(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// flush 组批：按 Key 轮询取段，凑满单次上限或字符上限即发上游。
func (b *batcher) flush(target string) {
	b.mu.Lock()
	tq := b.pending[target]
	if tq == nil {
		b.mu.Unlock()
		return
	}
	var batch []*batchItem
	chars := 0
	for len(batch) < cfg.DoubaoBatchMax {
		picked := false
		for i := 0; i < len(tq.order); i++ {
			key := tq.order[i]
			q := tq.queues[key]
			// 跳过已取消的项
			for len(q) > 0 && q[0].ctx.Err() != nil {
				q = q[1:]
			}
			tq.queues[key] = q
			if len(q) == 0 {
				continue
			}
			if len(batch) > 0 && chars+len(q[0].text)+1 > batchMaxChars {
				continue
			}
			batch = append(batch, q[0])
			chars += len(q[0].text) + 1
			tq.queues[key] = q[1:]
			// 轮询公平：取过一段的 Key 移到队尾
			tq.order = append(append(tq.order[:i:i], tq.order[i+1:]...), key)
			picked = true
			break // 重新从队头开始轮询各 Key
		}
		if !picked {
			break
		}
	}
	remaining := b.totalItems(tq) > 0
	if remaining {
		time.AfterFunc(batchWindow, func() { b.flush(target) })
	} else {
		delete(b.pending, target)
	}
	b.mu.Unlock()

	if len(batch) == 0 {
		return
	}

	texts := make([]string, len(batch))
	for i, it := range batch {
		texts[i] = it.text
	}
	items, perr := b.svc.callUpstream(texts, target)
	for i, it := range batch {
		if perr != nil {
			it.ch <- batchResult{perr: perr}
			continue
		}
		if v, ok := items[i]; ok {
			it.ch <- batchResult{text: v}
		} else {
			it.ch <- batchResult{perr: providerErr(
				fmt.Sprintf("该批缺少 index=%d 的译文，收到下标: %s", i, indexList(items)), 0, 502)}
		}
	}
}
