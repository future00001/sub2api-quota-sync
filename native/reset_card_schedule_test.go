package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func scheduledConfig() *resetCardSchedule {
	return &resetCardSchedule{
		ID: "11111111-1111-4111-8111-111111111111", Enabled: true, AccountID: 1,
		RunAt:           time.Now().Add(time.Hour).UTC().Format(time.RFC3339),
		CreditExpiresAt: time.Now().Add(8 * time.Hour).UTC().Format(time.RFC3339),
	}
}

func TestResetCardScheduleValidation(t *testing.T) {
	if err := validateResetCardSchedule(nil); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"valid", "id", "account", "timezone", "expiry"} {
		t.Run(field, func(t *testing.T) {
			s := scheduledConfig()
			switch field {
			case "id":
				s.ID = ""
			case "account":
				s.AccountID = 0
			case "timezone":
				s.RunAt = "2026-11-07T02:00:00"
			case "expiry":
				s.CreditExpiresAt = s.RunAt
			}
			err := validateResetCardSchedule(s)
			if (err == nil) != (field == "valid") {
				t.Fatalf("unexpected validation: %v", err)
			}
		})
	}
}

func TestResetCardScheduleLifecycle(t *testing.T) {
	s := &server{configPath: filepath.Join(t.TempDir(), configName)}
	task := scheduledConfig()
	if err := s.validateNewResetCardSchedule(task); err != nil {
		t.Fatal(err)
	}
	task.RunAt = time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)
	if err := s.validateNewResetCardSchedule(task); err == nil {
		t.Fatal("new past schedule accepted")
	}
	raw, _ := json.Marshal(Config{ResetCardSchedule: task})
	if err := os.WriteFile(s.configPath, raw, 0600); err != nil {
		t.Fatal(err)
	}
	if err := s.validateNewResetCardSchedule(task); err != nil {
		t.Fatal("restart cannot recover saved schedule", err)
	}
	changed := *task
	changed.AccountID = 2
	if err := s.validateNewResetCardSchedule(&changed); err == nil {
		t.Fatal("task mutation accepted")
	}
	task.Enabled = false
	raw, _ = json.Marshal(Config{ResetCardSchedule: task})
	_ = os.WriteFile(s.configPath, raw, 0600)
	task.Enabled = true
	if err := s.validateNewResetCardSchedule(task); err == nil {
		t.Fatal("closed task rearmed")
	}
}

func TestResetCardScheduleNormalize(t *testing.T) {
	raw, _ := json.Marshal(map[string]any{"reset_card_schedule": scheduledConfig()})
	_, cfg, err := normalizeConfig(raw)
	if err != nil || cfg.ResetCardSchedule == nil {
		t.Fatalf("schedule lost: %v", err)
	}
	if cfg.Enabled {
		t.Fatal("schedule must not enable subscription sync")
	}
}
