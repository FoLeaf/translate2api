package main

// SQLite 持久层：表结构与 Python 版完全一致，可从旧库直接迁移
// settings / credentials / api_keys / usage_log。

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

type Store struct {
	db *sql.DB
	mu sync.Mutex
}

const schema = `
CREATE TABLE IF NOT EXISTS settings(
	key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(
	provider TEXT NOT NULL, name TEXT NOT NULL,
	data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
	created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
	PRIMARY KEY(provider, name));
CREATE TABLE IF NOT EXISTS api_keys(
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
	prefix TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
	created_at INTEGER NOT NULL, last_used_at INTEGER);
CREATE TABLE IF NOT EXISTS usage_log(
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	ts INTEGER NOT NULL, provider TEXT NOT NULL, endpoint TEXT NOT NULL,
	ok INTEGER NOT NULL, code TEXT, chars INTEGER, ms INTEGER,
	ip TEXT, msg TEXT);
`

func OpenStore(dataDir string) (*Store, error) {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", filepath.Join(dataDir, "transhub-go.db"))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	for _, pragma := range []string{
		"PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=5000", "PRAGMA synchronous=NORMAL",
	} {
		if _, err := db.Exec(pragma); err != nil {
			return nil, fmt.Errorf("%s: %w", pragma, err)
		}
	}
	if _, err := db.Exec(schema); err != nil {
		return nil, err
	}
	return &Store{db: db}, nil
}

// ---------------------------------------------------------------- settings

func (s *Store) GetSetting(key string) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var v string
	err := s.db.QueryRow("SELECT value FROM settings WHERE key=?", key).Scan(&v)
	return v, err == nil
}

func (s *Store) GetSettingJSON(key string, out any) bool {
	raw, ok := s.GetSetting(key)
	if !ok {
		return false
	}
	return json.Unmarshal([]byte(raw), out) == nil
}

func (s *Store) SetSetting(key string, value any) error {
	raw, err := json.Marshal(value)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err = s.db.Exec("INSERT INTO settings(key,value) VALUES(?,?) "+
		"ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, string(raw))
	return err
}

func (s *Store) GetOrCreateSecret() (string, error) {
	if v, ok := s.GetSetting("secret"); ok && v != "" && v != `""` {
		var sec string
		if json.Unmarshal([]byte(v), &sec) == nil && sec != "" {
			return sec, nil
		}
	}
	sec := randTokenURLSafe(32)
	if err := s.SetSetting("secret", sec); err != nil {
		return "", err
	}
	return sec, nil
}

// ------------------------------------------------------------- credentials

type Credential struct {
	Provider   string
	Name       string
	Data       map[string]any
	Status     string
	CreatedAt  int64
	UpdatedAt  int64
}

func (s *Store) PutCredential(provider string, data map[string]any, name, status string) error {
	if name == "" {
		name = "default"
	}
	if status == "" {
		status = "active"
	}
	raw, err := json.Marshal(data)
	if err != nil {
		return err
	}
	now := time.Now().Unix()
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err = s.db.Exec(
		"INSERT INTO credentials(provider,name,data,status,created_at,updated_at) "+
			"VALUES(?,?,?,?,?,?) ON CONFLICT(provider,name) DO UPDATE SET "+
			"data=excluded.data, status=excluded.status, updated_at=excluded.updated_at",
		provider, name, string(raw), status, now, now)
	return err
}

// GetCredential 取可用凭据：优先 status=active 的最新一条。
func (s *Store) GetCredential(provider string) (*Credential, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	row := s.db.QueryRow(
		"SELECT provider,name,data,status,created_at,updated_at FROM credentials "+
			"WHERE provider=? AND status='active' ORDER BY updated_at DESC LIMIT 1", provider)
	c, err := scanCredential(row)
	if err == sql.ErrNoRows {
		row = s.db.QueryRow(
			"SELECT provider,name,data,status,created_at,updated_at FROM credentials "+
				"WHERE provider=? ORDER BY updated_at DESC LIMIT 1", provider)
		c, err = scanCredential(row)
	}
	if err != nil {
		return nil, err
	}
	return c, nil
}

func (s *Store) ListCredentials(provider string) ([]*Credential, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var rows *sql.Rows
	var err error
	if provider != "" {
		rows, err = s.db.Query("SELECT provider,name,data,status,created_at,updated_at "+
			"FROM credentials WHERE provider=? ORDER BY updated_at DESC", provider)
	} else {
		rows, err = s.db.Query("SELECT provider,name,data,status,created_at,updated_at "+
			"FROM credentials ORDER BY updated_at DESC")
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Credential
	for rows.Next() {
		c, err := scanCredential(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func scanCredential(sc interface{ Scan(...any) error }) (*Credential, error) {
	var c Credential
	var raw string
	if err := sc.Scan(&c.Provider, &c.Name, &raw, &c.Status, &c.CreatedAt, &c.UpdatedAt); err != nil {
		return nil, err
	}
	c.Data = map[string]any{}
	_ = json.Unmarshal([]byte(raw), &c.Data)
	return &c, nil
}

func (s *Store) SetCredentialStatus(provider, status string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("UPDATE credentials SET status=?, updated_at=? WHERE provider=?",
		status, time.Now().Unix(), provider)
	return err
}

func (s *Store) DeleteCredential(provider, name string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.Exec("DELETE FROM credentials WHERE provider=? AND name=?", provider, name)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

// ---------------------------------------------------------------- api keys

func (s *Store) CreateAPIKey(name string) (int64, string, error) {
	full := "th-" + randTokenURLSafe(24)
	khash := sha256Hex(full)
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.Exec(
		"INSERT INTO api_keys(name,key_hash,prefix,created_at) VALUES(?,?,?,?)",
		name, khash, clip(full, 9)+"…", time.Now().Unix())
	if err != nil {
		return 0, "", err
	}
	id, _ := res.LastInsertId()
	return id, full, nil
}

type APIKeyRow struct {
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	Prefix    string `json:"prefix"`
	Enabled   bool   `json:"enabled"`
	CreatedAt int64  `json:"created_at"`
	LastUsed  any    `json:"last_used_at"`
}

func (s *Store) ListAPIKeys() ([]APIKeyRow, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.Query("SELECT id,name,prefix,enabled,created_at,last_used_at " +
		"FROM api_keys ORDER BY id DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []APIKeyRow
	for rows.Next() {
		var r APIKeyRow
		var enabled int
		var last sql.NullInt64
		if err := rows.Scan(&r.ID, &r.Name, &r.Prefix, &enabled, &r.CreatedAt, &last); err != nil {
			return nil, err
		}
		r.Enabled = enabled == 1
		if last.Valid {
			r.LastUsed = last.Int64
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Store) VerifyAPIKey(token string) (int64, string, bool) {
	if token == "" {
		return 0, "", false
	}
	khash := sha256Hex(token)
	s.mu.Lock()
	defer s.mu.Unlock()
	var id int64
	var name string
	var enabled int
	err := s.db.QueryRow("SELECT id,name,enabled FROM api_keys WHERE key_hash=?", khash).
		Scan(&id, &name, &enabled)
	if err != nil || enabled != 1 {
		return 0, "", false
	}
	return id, name, true
}

// TouchAPIKey 由后台协程定期批量刷新 last_used_at，避免热路径每请求写库。
func (s *Store) TouchAPIKey(id int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.db.Exec("UPDATE api_keys SET last_used_at=? WHERE id=?", time.Now().Unix(), id)
}

func (s *Store) KeyEnabledExists() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	var n int
	_ = s.db.QueryRow("SELECT COUNT(*) FROM api_keys WHERE enabled=1").Scan(&n)
	return n > 0
}

func (s *Store) SetAPIKeyEnabled(id int64, enabled bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := 0
	if enabled {
		v = 1
	}
	_, err := s.db.Exec("UPDATE api_keys SET enabled=? WHERE id=?", v, id)
	return err
}

func (s *Store) DeleteAPIKey(id int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("DELETE FROM api_keys WHERE id=?", id)
	return err
}

// -------------------------------------------------------------- usage log

func (s *Store) LogUsage(provider, endpoint string, ok bool, code *string,
	chars *int64, ms *int64, ip, msg string) {
	if len(msg) > 300 {
		msg = msg[:300]
	}
	okv := 0
	if ok {
		okv = 1
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	_, _ = s.db.Exec(
		"INSERT INTO usage_log(ts,provider,endpoint,ok,code,chars,ms,ip,msg) "+
			"VALUES(?,?,?,?,?,?,?,?,?)",
		time.Now().Unix(), provider, endpoint, okv, code, chars, ms, ip, msg)
}

func (s *Store) RecentUsage(limit int) ([]map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.Query("SELECT * FROM usage_log ORDER BY id DESC LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols, _ := rows.Columns()
	var out []map[string]any
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, err
		}
		m := map[string]any{}
		for i, c := range cols {
			v := vals[i]
			if b, ok := v.([]byte); ok {
				v = string(b)
			}
			m[c] = v
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

type ProviderStat struct {
	Provider string  `json:"provider"`
	Count    int64   `json:"cnt"`
	OK       int64   `json:"ok"`
	Chars    int64   `json:"chars"`
	AvgMs    float64 `json:"avg_ms"`
}

func (s *Store) UsageStats(sinceSeconds int64) ([]ProviderStat, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.Query(
		"SELECT provider, COUNT(*), SUM(ok), SUM(COALESCE(chars,0)), "+
			"AVG(COALESCE(ms,0)) FROM usage_log WHERE ts>=? GROUP BY provider",
		time.Now().Unix()-sinceSeconds)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ProviderStat
	for rows.Next() {
		var st ProviderStat
		if err := rows.Scan(&st.Provider, &st.Count, &st.OK, &st.Chars, &st.AvgMs); err != nil {
			return nil, err
		}
		out = append(out, st)
	}
	return out, rows.Err()
}

// ---------------------------------------------------------------- 迁移

// MigrateFrom 从 Python 版数据库迁移 Key（sha256 直接搬，用户零改动）、
// 设置与凭据；用量历史按共识不迁移。
func (s *Store) MigrateFrom(oldPath string) (string, error) {
	if _, err := os.Stat(oldPath); err != nil {
		return "", nil // 没有旧库，全新部署
	}
	var keyCount int
	if err := s.db.QueryRow("SELECT COUNT(*) FROM api_keys").Scan(&keyCount); err != nil {
		return "", err
	}
	if keyCount > 0 {
		return "", nil // 已迁移过
	}
	old, err := sql.Open("sqlite", oldPath)
	if err != nil {
		return "", err
	}
	defer old.Close()

	migrated := []string{}

	if rows, err := old.Query("SELECT name,key_hash,prefix,enabled,created_at,last_used_at FROM api_keys"); err == nil {
		n := 0
		for rows.Next() {
			var name, khash, prefix string
			var enabled int
			var created int64
			var last sql.NullInt64
			if err := rows.Scan(&name, &khash, &prefix, &enabled, &created, &last); err == nil {
				_, _ = s.db.Exec("INSERT OR IGNORE INTO api_keys(name,key_hash,prefix,enabled,created_at,last_used_at) "+
					"VALUES(?,?,?,?,?,?)", name, khash, prefix, enabled, created, last)
				n++
			}
		}
		rows.Close()
		if n > 0 {
			migrated = append(migrated, fmt.Sprintf("api_keys %d 条", n))
		}
	}

	settingKeys := []string{"admin_password_hash", "secret", "doubao_engine",
		"doubao_scene", "rate_bilibili", "rate_doubao"}
	n := 0
	for _, k := range settingKeys {
		var v string
		if err := old.QueryRow("SELECT value FROM settings WHERE key=?", k).Scan(&v); err == nil {
			if _, err := s.db.Exec("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", k, v); err == nil {
				n++
			}
		}
	}
	if n > 0 {
		migrated = append(migrated, fmt.Sprintf("settings %d 项", n))
	}

	if rows, err := old.Query("SELECT provider,name,data,status,created_at,updated_at FROM credentials"); err == nil {
		n := 0
		for rows.Next() {
			var provider, name, data, status string
			var created, updated int64
			if err := rows.Scan(&provider, &name, &data, &status, &created, &updated); err == nil {
				_, _ = s.db.Exec("INSERT OR IGNORE INTO credentials(provider,name,data,status,created_at,updated_at) "+
					"VALUES(?,?,?,?,?,?)", provider, name, data, status, created, updated)
				n++
			}
		}
		rows.Close()
		if n > 0 {
			migrated = append(migrated, fmt.Sprintf("credentials %d 条", n))
		}
	}

	if len(migrated) == 0 {
		return "", nil
	}
	return "已从 Python 版迁移: " + joinStrings(migrated, "，"), nil
}

func joinStrings(parts []string, sep string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += sep
		}
		out += p
	}
	return out
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
