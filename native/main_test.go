package main

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pluginv1 "sub2api-quota-sync-native/pluginapi"

	"google.golang.org/grpc/metadata"
)

type fakeForwardStream struct {
	ctx       context.Context
	requests  []*pluginv1.ForwardRequest
	responses []*pluginv1.ForwardResponse
	index     int
}

func (s *fakeForwardStream) Send(response *pluginv1.ForwardResponse) error {
	s.responses = append(s.responses, response)
	return nil
}

func (s *fakeForwardStream) Recv() (*pluginv1.ForwardRequest, error) {
	if s.index >= len(s.requests) {
		return nil, io.EOF
	}
	request := s.requests[s.index]
	s.index++
	return request, nil
}

func (s *fakeForwardStream) SetHeader(metadata.MD) error  { return nil }
func (s *fakeForwardStream) SendHeader(metadata.MD) error { return nil }
func (s *fakeForwardStream) SetTrailer(metadata.MD)       {}
func (s *fakeForwardStream) Context() context.Context     { return s.ctx }
func (s *fakeForwardStream) SendMsg(any) error            { return nil }
func (s *fakeForwardStream) RecvMsg(any) error            { return nil }

func TestNormalizeDefaults(t *testing.T) {
	raw, cfg, err := normalizeConfig([]byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Enabled || !cfg.DryRun || !cfg.ResetWeekly || len(cfg.AccountIDs) != 1 || cfg.AccountIDs[0] != 1 || cfg.MinCycleShiftSeconds != 24*60*60 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
	if len(raw) == 0 {
		t.Fatal("normalized config is empty")
	}
}

func TestNormalizeGroupNameAndID(t *testing.T) {
	_, cfg, err := normalizeConfig([]byte(`{
		"enabled":true,"dry_run":true,"account_ids":[1,2,1],
		"target_groups":["GPT订阅550每周","4","GPT订阅550每周"],
		"reset_weekly":true,
		"min_cycle_shift_seconds":259200,
		"rearm_remaining_seconds":432000,
		"jitter_tolerance_seconds":21600
	}`))
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.TargetGroups) != 2 {
		t.Fatalf("expected deduplicated groups, got %#v", cfg.TargetGroups)
	}
	if len(cfg.AccountIDs) != 2 || cfg.AccountIDs[0] != 1 || cfg.AccountIDs[1] != 2 {
		t.Fatalf("expected deduplicated accounts, got %#v", cfg.AccountIDs)
	}
}

func TestNormalizeRejectsEnabledWithoutGroup(t *testing.T) {
	_, _, err := normalizeConfig([]byte(`{"enabled":true}`))
	if err == nil {
		t.Fatal("expected validation error")
	}
}

func TestNormalizeRejectsDailyOnly(t *testing.T) {
	_, _, err := normalizeConfig([]byte(`{
		"enabled":true,"account_ids":[1],"target_groups":["4"],
		"reset_daily":true,"reset_weekly":false,"reset_monthly":false,
		"min_cycle_shift_seconds":86400,
		"rearm_remaining_seconds":432000,
		"jitter_tolerance_seconds":21600
	}`))
	if err == nil {
		t.Fatal("expected daily-only validation error")
	}
}

func TestNormalizeRejectsUnknownField(t *testing.T) {
	_, _, err := normalizeConfig([]byte(`{"unknown":true}`))
	if err == nil {
		t.Fatal("expected unknown-field error")
	}
}

func TestNormalizeModeAccepted(t *testing.T) {
	for _, mode := range []string{"subscription", "balance"} {
		_, cfg, err := normalizeConfig([]byte(`{"mode":"` + mode + `"}`))
		if err != nil {
			t.Fatalf("mode %q should be accepted: %v", mode, err)
		}
		if cfg.Mode != mode {
			t.Fatalf("expected mode %q, got %q", mode, cfg.Mode)
		}
	}
}

func TestNormalizeEmptyModeDefaultsToSubscription(t *testing.T) {
	raw, cfg, err := normalizeConfig([]byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Mode != "subscription" {
		t.Fatalf("expected empty mode to normalize to subscription, got %q", cfg.Mode)
	}
	if !bytes.Contains(raw, []byte(`"mode":"subscription"`)) {
		t.Fatalf("expected mode persisted in normalized config: %s", raw)
	}
}

func TestNormalizeRejectsInvalidMode(t *testing.T) {
	_, _, err := normalizeConfig([]byte(`{"mode":"weekly"}`))
	if err == nil {
		t.Fatal("expected invalid-mode error")
	}
}

func TestNormalizeModeRoundTrips(t *testing.T) {
	raw, cfg, err := normalizeConfig([]byte(`{"mode":"balance"}`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Mode != "balance" {
		t.Fatalf("expected mode balance, got %q", cfg.Mode)
	}
	_, again, err := normalizeConfig(raw)
	if err != nil {
		t.Fatal(err)
	}
	if again.Mode != "balance" {
		t.Fatalf("expected mode to survive re-normalization, got %q", again.Mode)
	}
}

func TestLegacyAccountIDMigratesToStableConfig(t *testing.T) {
	legacy, _, err := normalizeConfig([]byte(`{"account_id":1}`))
	if err != nil {
		t.Fatal(err)
	}
	modern, _, err := normalizeConfig([]byte(`{"account_ids":[1]}`))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(legacy, modern) {
		t.Fatalf("legacy and modern config differ:\n%s\n%s", legacy, modern)
	}
}

func TestForwardStreamsRequestAndResponse(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Error(err)
		}
		if request.Method != http.MethodPost || string(body) != "hello world" || request.Header.Get("Authorization") != "Bearer test" {
			t.Errorf("unexpected upstream request: method=%s body=%q auth=%q", request.Method, body, request.Header.Get("Authorization"))
		}
		writer.Header().Add("X-Test", "one")
		writer.Header().Add("X-Test", "two")
		writer.WriteHeader(http.StatusCreated)
		_, _ = writer.Write([]byte("streamed response"))
	}))
	defer upstream.Close()

	stream := &fakeForwardStream{
		ctx: context.Background(),
		requests: []*pluginv1.ForwardRequest{
			{Frame: &pluginv1.ForwardRequest_Start{Start: &pluginv1.ForwardRequestStart{
				Method: http.MethodPost, Url: upstream.URL, Host: strings.TrimPrefix(upstream.URL, "http://"),
				Headers:       map[string]*pluginv1.HeaderValues{"Authorization": {Values: []string{"Bearer test"}}},
				ContentLength: 11, HasBody: true,
			}}},
			{Frame: &pluginv1.ForwardRequest_BodyChunk{BodyChunk: []byte("hello ")}},
			{Frame: &pluginv1.ForwardRequest_BodyChunk{BodyChunk: []byte("world")}},
			{Frame: &pluginv1.ForwardRequest_BodyEnd{BodyEnd: true}},
		},
	}
	srv := &server{transports: make(map[string]*http.Transport)}
	if err := srv.Forward(stream); err != nil {
		t.Fatal(err)
	}
	if len(stream.responses) < 3 || stream.responses[0].GetStart().GetStatusCode() != http.StatusCreated {
		t.Fatalf("unexpected response frames: %#v", stream.responses)
	}
	var body []byte
	for _, response := range stream.responses {
		body = append(body, response.GetBodyChunk()...)
	}
	if string(body) != "streamed response" {
		t.Fatalf("unexpected response body: %q", body)
	}
	end := stream.responses[len(stream.responses)-1].GetEnd()
	if end == nil || end.GetBytesReceived() != int64(len(body)) {
		t.Fatalf("unexpected end frame: %#v", end)
	}
}

func TestForwardRejectsMissingStartFrame(t *testing.T) {
	stream := &fakeForwardStream{ctx: context.Background(), requests: []*pluginv1.ForwardRequest{{Frame: &pluginv1.ForwardRequest_BodyEnd{BodyEnd: true}}}}
	srv := &server{transports: make(map[string]*http.Transport)}
	if err := srv.Forward(stream); err != nil {
		t.Fatal(err)
	}
	if len(stream.responses) != 1 || stream.responses[0].GetError().GetCode() != "INVALID_REQUEST_STREAM" || stream.responses[0].GetError().GetRequestSent() {
		t.Fatalf("unexpected error response: %#v", stream.responses)
	}
}
