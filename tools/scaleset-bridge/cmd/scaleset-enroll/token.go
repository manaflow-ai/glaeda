package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"syscall"
	"time"
)

const maxTokenFileBytes = 16 * 1024
const minTokenLife = 5 * time.Minute
const runnerPermission = "organization_self_hosted_runners"

// tokenFile is the exact document the App key's home host pushes over ssh (hq
// tools/lane-gh-broker read_token_push): one installation token, never the App key.
type tokenFile struct {
	AppID       int64             `json:"app_id"`
	Token       string            `json:"token"`
	ExpiresAt   string            `json:"expires_at"`
	Permissions map[string]string `json:"permissions"`
	MintedAt    string            `json:"minted_at"`
}

type installationToken struct {
	value     string
	expiresAt time.Time
}

// readTokenFile reads the pushed token without following a link, and only from a regular, single-link
// file this user owns with mode 0600 or 0400. The token must belong to the expected App, carry
// organization self-hosted runner write, and live at least minTokenLife past now.
func readTokenFile(path string, expectedAppID int64, now time.Time) (installationToken, error) {
	if path == "" || path[0] != '/' {
		return installationToken{}, errors.New("token_file_path_invalid")
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if err != nil {
		return installationToken{}, errors.New("token_file_unavailable")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return installationToken{}, errors.New("token_file_unavailable")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	mode := info.Mode()
	if !ok || !mode.IsRegular() || int(stat.Uid) != os.Geteuid() || stat.Nlink != 1 ||
		(mode.Perm() != 0o600 && mode.Perm() != 0o400) || mode&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 ||
		info.Size() == 0 || info.Size() > maxTokenFileBytes {
		return installationToken{}, errors.New("token_file_unsafe")
	}
	raw, err := io.ReadAll(io.LimitReader(file, maxTokenFileBytes+1))
	if err != nil || len(raw) > maxTokenFileBytes {
		return installationToken{}, errors.New("token_file_unavailable")
	}
	defer clear(raw)
	var document tokenFile
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&document); err != nil {
		return installationToken{}, errors.New("token_file_invalid")
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return installationToken{}, errors.New("token_file_invalid")
	}
	if document.AppID != expectedAppID {
		return installationToken{}, errors.New("token_file_wrong_app")
	}
	if !boundedToken(document.Token, 1024) {
		return installationToken{}, errors.New("token_file_invalid")
	}
	if document.Permissions[runnerPermission] != "write" {
		return installationToken{}, errors.New("token_file_lacks_runner_write")
	}
	expiresAt, err := time.Parse(time.RFC3339, document.ExpiresAt)
	if err != nil {
		return installationToken{}, errors.New("token_file_invalid")
	}
	if expiresAt.Sub(now) < minTokenLife {
		return installationToken{}, errors.New("token_file_expiring")
	}
	return installationToken{value: document.Token, expiresAt: expiresAt}, nil
}

func boundedToken(value string, maximum int) bool {
	if len(value) == 0 || len(value) > maximum {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character > 0x7e {
			return false
		}
	}
	return true
}
