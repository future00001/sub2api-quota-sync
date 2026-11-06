package main

import (
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"time"
)

// 每次显式预约使用独立 ID；普通保存保留 ID，防止重复消费。
type resetCardSchedule struct {
	ID              string `json:"id"`
	Enabled         bool   `json:"enabled"`
	AccountID       int64  `json:"account_id"`
	RunAt           string `json:"run_at"`
	CreditExpiresAt string `json:"credit_expires_at"`
	Result          string `json:"result,omitempty"`
}

var scheduleIDPattern = regexp.MustCompile(`^[a-fA-F0-9-]{36}$`)

func validateResetCardSchedule(s *resetCardSchedule) error {
	if s == nil || !s.Enabled {
		return nil
	}
	if !scheduleIDPattern.MatchString(s.ID) || s.AccountID <= 0 {
		return errors.New("定时用卡需要有效的任务 ID 和账号")
	}
	run, err := time.Parse(time.RFC3339Nano, s.RunAt)
	if err != nil {
		return errors.New("执行时间必须包含时区")
	}
	expires, err := time.Parse(time.RFC3339Nano, s.CreditExpiresAt)
	if err != nil || !run.Before(expires) {
		return errors.New("执行时间必须早于重置卡到期时间；请先在账号页查询重置卡并刷新选项")
	}
	return nil
}

func (s *server) validateNewResetCardSchedule(next *resetCardSchedule) error {
	if next == nil || !next.Enabled {
		return nil
	}
	var previous Config
	raw, err := os.ReadFile(s.configPath)
	if err == nil {
		_ = json.Unmarshal(raw, &previous)
	}
	old := previous.ResetCardSchedule
	if old != nil && old.ID == next.ID {
		if old.AccountID != next.AccountID || old.RunAt != next.RunAt || old.CreditExpiresAt != next.CreditExpiresAt {
			return errors.New("修改定时任务必须生成新的任务 ID")
		}
		if !old.Enabled {
			return errors.New("该单次任务已关闭，请重新开启并选择未来时间")
		}
		return nil
	}
	run, _ := time.Parse(time.RFC3339Nano, next.RunAt)
	if !run.After(time.Now()) {
		return errors.New("请选择未来的执行时间（北京时间）")
	}
	return nil
}
