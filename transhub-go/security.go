package main

// 安全组件：scrypt 密码哈希（与 Python 版同格式，可互认）、
// HMAC 会话令牌（同格式，迁移后已有会话 Cookie 仍有效）、
// API Key 头解析、登录防爆破。

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/scrypt"
)

func randRead(b []byte) (int, error) { return rand.Read(b) }

func randTokenURLSafe(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// ---------------------------------------------------------------- 密码

func HashPassword(password string) string {
	salt := make([]byte, 16)
	_, _ = rand.Read(salt)
	h, _ := scrypt.Key([]byte(password), salt, 1<<14, 8, 1, 32)
	return "scrypt$" + hex.EncodeToString(salt) + "$" + hex.EncodeToString(h)
}

func VerifyPassword(password, stored string) bool {
	parts := strings.Split(stored, "$")
	if len(parts) != 3 || parts[0] != "scrypt" {
		return false
	}
	salt, err1 := hex.DecodeString(parts[1])
	want, err2 := hex.DecodeString(parts[2])
	if err1 != nil || err2 != nil {
		return false
	}
	got, err := scrypt.Key([]byte(password), salt, 1<<14, 8, 1, 32)
	if err != nil {
		return false
	}
	return subtle.ConstantTimeCompare(got, want) == 1
}

// ---------------------------------------------------------------- 会话

func issueSession(secret string, days int) string {
	exp := time.Now().Unix() + int64(days)*86400
	payload := strconv.FormatInt(exp, 10)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(payload))
	return payload + "." + hex.EncodeToString(mac.Sum(nil))
}

func checkSession(secret, token string) bool {
	if token == "" || !strings.Contains(token, ".") {
		return false
	}
	idx := strings.LastIndex(token, ".")
	payload, sig := token[:idx], token[idx+1:]
	if _, err := strconv.ParseInt(payload, 10, 64); err != nil {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(payload))
	expect := hex.EncodeToString(mac.Sum(nil))
	if subtle.ConstantTimeCompare([]byte(sig), []byte(expect)) != 1 {
		return false
	}
	exp, _ := strconv.ParseInt(payload, 10, 64)
	return exp > time.Now().Unix()
}

// ---------------------------------------------------------------- API Key

// parseAuthorization 兼容 Bearer / DeepL-Auth-Key / 裸 key 三种写法。
func parseAuthorization(header string) string {
	token := strings.TrimSpace(header)
	if token == "" {
		return ""
	}
	low := strings.ToLower(token)
	for _, prefix := range []string{"bearer ", "deepl-auth-key "} {
		if strings.HasPrefix(low, prefix) {
			return strings.TrimSpace(token[len(prefix):])
		}
	}
	return token
}

// --------------------------------------------------------- 登录防爆破

const (
	loginWindow   = 15 * time.Minute
	loginMaxFails = 8
)

var loginFails struct {
	mu    sync.Mutex
	fails map[string][]time.Time
}

func init() { loginFails.fails = map[string][]time.Time{} }

func loginAttemptsExceeded(ip string) bool {
	loginFails.mu.Lock()
	defer loginFails.mu.Unlock()
	now := time.Now()
	var keep []time.Time
	for _, t := range loginFails.fails[ip] {
		if now.Sub(t) < loginWindow {
			keep = append(keep, t)
		}
	}
	loginFails.fails[ip] = keep
	return len(keep) >= loginMaxFails
}

func recordLoginFail(ip string) {
	loginFails.mu.Lock()
	defer loginFails.mu.Unlock()
	loginFails.fails[ip] = append(loginFails.fails[ip], time.Now())
}

func recordLoginOK(ip string) {
	loginFails.mu.Lock()
	defer loginFails.mu.Unlock()
	delete(loginFails.fails, ip)
}

// --------------------------------------------------------- 限流令牌桶

type tokenBucket struct {
	rate     float64
	capacity float64
	tokens   float64
	last     time.Time
}

func (b *tokenBucket) acquire() bool {
	now := time.Now()
	b.tokens = minFloat(b.capacity, b.tokens+now.Sub(b.last).Seconds()*b.rate)
	b.last = now
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

func minFloat(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}

