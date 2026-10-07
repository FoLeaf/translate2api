package main

// B站 Index-Translate 适配层：对外仍是 OpenAI 兼容接口（ReadFrog 零改动），
// 对内改走网页端仍在服务的 /?p=/translate_stream 端点。
// 背景：2026-10-05 B站部署后 /v1/chat/completions 全模型 500（双出口实测），
// 网页端正常因其走 translate_stream，模型名换为 index-mt-2b/9b/35b。
// 该端点返回标准 OpenAI chat.completion.chunk 形态的 SSE：
// 流式请求直接透传分块，非流式请求聚合为完整 chat.completion JSON。

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"
)

const biliStreamAPIPath = "/?p=/translate_stream"

// mapBiliModel 兼容旧模型名（Index-Translate-2B/9B/35B-A3B）与新名（index-mt-*）。
func mapBiliModel(name string) string {
	low := strings.ToLower(strings.TrimSpace(name))
	switch {
	case low == "index-mt-35b" || strings.Contains(low, "35"):
		return "index-mt-35b"
	case low == "index-mt-9b" || strings.Contains(low, "9b") || strings.Contains(low, "9B"):
		return "index-mt-9b"
	default:
		return "index-mt-2b"
	}
}

type openaiChatRequest struct {
	Model    string `json:"model"`
	Stream   bool   `json:"stream"`
	Messages []struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	} `json:"messages"`
}

// biliTargetLang 目标语言：OpenAI 请求不携带语言字段，而网页端模板需要显式
// 目标语言；本网关用户群为中文用户，默认 zh，可在后台设置 bili_target_lang 覆盖。
func biliTargetLang() string {
	var v string
	if store.GetSettingJSON("bili_target_lang", &v) && v != "" {
		return v
	}
	return "zh"
}

// lastUserContent 取最后一条用户消息内容（ReadFrog 把待译文本放在 user 消息里）。
func lastUserContent(req *openaiChatRequest) string {
	for i := len(req.Messages) - 1; i >= 0; i-- {
		if req.Messages[i].Content != "" {
			return req.Messages[i].Content
		}
	}
	return ""
}

// ProxyChat 处理 OpenAI 兼容的 chat/completions：翻译到 translate_stream 再回填。
func (s *BiliService) ProxyChat(w http.ResponseWriter, r *http.Request) ProxyOutcome {
	// 上游并发信号量：与透传路径共用
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

	raw, err := io.ReadAll(io.LimitReader(r.Body, 32<<20))
	if err != nil {
		writeJSON(w, 400, map[string]any{"error": map[string]any{
			"message": "读取请求体失败", "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 400, Err: "read body: " + err.Error()}
	}
	var oai openaiChatRequest
	if err := json.Unmarshal(raw, &oai); err != nil {
		writeJSON(w, 400, map[string]any{"error": map[string]any{
			"message": "请求体不是合法的 OpenAI chat 请求", "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 400, Err: "bad json"}
	}
	text := lastUserContent(&oai)
	if text == "" {
		writeJSON(w, 400, map[string]any{"error": map[string]any{
			"message": "缺少待翻译文本", "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 400, Err: "empty content"}
	}

	upstreamBody, _ := json.Marshal(map[string]any{
		"text":        text,
		"source_lang": "auto",
		"target_lang": biliTargetLang(),
		"model":       mapBiliModel(oai.Model),
		"stream":      true, // 上游恒为 SSE；流式与否由本层决定透传还是聚合
	})
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost,
		cfg.BilibiliUpstream+biliStreamAPIPath, bytes.NewReader(upstreamBody))
	if err != nil {
		writeJSON(w, 502, map[string]any{"error": map[string]any{
			"message": "构造上游请求失败", "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 502, Err: err.Error()}
	}
	req.Header.Set("Content-Type", "application/json")
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

	if resp.StatusCode != 200 {
		// 上游错误直接返回（含状态码与原文）
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		h := w.Header()
		h.Set("Content-Type", resp.Header.Get("Content-Type"))
		for k, v := range corsHeaders(nil) {
			h.Set(k, v)
		}
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(body)
		return ProxyOutcome{Status: resp.StatusCode, Err: clip(string(body), 200)}
	}

	if oai.Stream {
		return s.passthroughSSE(w, resp)
	}
	return s.aggregateSSE(w, resp, mapBiliModel(oai.Model))
}

// passthroughSSE 流式透传：上游分块本就是 OpenAI chunk 形态。
func (s *BiliService) passthroughSSE(w http.ResponseWriter, resp *http.Response) ProxyOutcome {
	h := w.Header()
	if ct := resp.Header.Get("Content-Type"); ct != "" {
		h.Set("Content-Type", ct)
	} else {
		h.Set("Content-Type", "text/event-stream; charset=utf-8")
	}
	h.Set("Cache-Control", "no-cache")
	for k, v := range corsHeaders(nil) {
		h.Set(k, v)
	}
	w.WriteHeader(http.StatusOK)

	var total int64
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 16*1024)
	for {
		n, rerr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return ProxyOutcome{Status: 200, Bytes: total, Err: "client write: " + werr.Error()}
			}
			total += int64(n)
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr == io.EOF {
			return ProxyOutcome{Status: 200, Bytes: total}
		}
		if rerr != nil {
			return ProxyOutcome{Status: 200, Bytes: total, Err: "upstream read: " + rerr.Error()}
		}
	}
}

type sseChunk struct {
	ID      string `json:"id"`
	Created int64  `json:"created"`
	Model   string `json:"model"`
	Choices []struct {
		Delta struct {
			Content string `json:"content"`
		} `json:"delta"`
		FinishReason any `json:"finish_reason"`
	} `json:"choices"`
}

// aggregateSSE 非流式：聚合 delta.content 为一条完整 chat.completion。
func (s *BiliService) aggregateSSE(w http.ResponseWriter, resp *http.Response, model string) ProxyOutcome {
	var sb strings.Builder
	var id, finModel string
	var created int64
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, ":") {
			continue
		}
		data, ok := strings.CutPrefix(line, "data:")
		if !ok {
			continue
		}
		data = strings.TrimSpace(data)
		if data == "[DONE]" {
			break
		}
		var chunk sseChunk
		if err := json.Unmarshal([]byte(data), &chunk); err != nil {
			continue
		}
		if chunk.ID != "" {
			id = chunk.ID
		}
		if chunk.Model != "" {
			finModel = chunk.Model
		}
		if chunk.Created != 0 {
			created = chunk.Created
		}
		for _, ch := range chunk.Choices {
			sb.WriteString(ch.Delta.Content)
		}
	}
	if err := scanner.Err(); err != nil {
		writeJSON(w, 502, map[string]any{"error": map[string]any{
			"message": "聚合上游流失败: " + err.Error(), "type": "proxy_error"}}, nil)
		return ProxyOutcome{Status: 502, Err: err.Error()}
	}
	if finModel == "" {
		finModel = model
	}
	if id == "" {
		id = "chatcmpl-transhub"
	}
	if created == 0 {
		created = time.Now().Unix()
	}
	out := map[string]any{
		"id":      id,
		"object":  "chat.completion",
		"created": created,
		"model":   finModel,
		"choices": []map[string]any{{
			"index": 0,
			"message": map[string]any{
				"role":    "assistant",
				"content": sb.String(),
			},
			"finish_reason": "stop",
		}},
	}
	writeJSON(w, 200, out, nil)
	return ProxyOutcome{Status: 200, Bytes: int64(sb.Len())}
}
