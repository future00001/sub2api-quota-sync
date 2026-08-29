package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	pluginv1 "sub2api-quota-sync-native/pluginapi"

	"google.golang.org/grpc"
)

const (
	pluginID      = "com.hzyhz.sub2api-quota-sync"
	pluginVersion = "0.3.1"
	capabilityID  = "openai.oauth.outbound_transport.v1"
	configName    = "quota-sync-config.json"
	catalogName   = "quota-sync-catalog.json"
)

type Config struct {
	Enabled                bool           `json:"enabled"`
	DryRun                 bool           `json:"dry_run"`
	AccountIDs             []int64        `json:"account_ids"`
	LegacyAccountID        int64          `json:"account_id,omitempty"`
	TargetGroups           []string       `json:"target_groups"`
	Catalog                *optionCatalog `json:"catalog,omitempty"`
	ResetDaily             bool           `json:"reset_daily"`
	ResetWeekly            bool           `json:"reset_weekly"`
	ResetMonthly           bool           `json:"reset_monthly"`
	MinCycleShiftSeconds   int            `json:"min_cycle_shift_seconds"`
	RearmRemainingSeconds  int            `json:"rearm_remaining_seconds"`
	JitterToleranceSeconds int            `json:"jitter_tolerance_seconds"`
}

type executorStatus struct {
	ConfigMtimeNS         int64   `json:"config_mtime_ns"`
	ConfigSHA256          string  `json:"config_sha256"`
	OK                    bool    `json:"ok"`
	Enabled               bool    `json:"enabled"`
	DryRun                bool    `json:"dry_run"`
	ResolvedGroupIDs      []int64 `json:"resolved_group_ids"`
	SelectedAccountIDs    []int64 `json:"selected_account_ids"`
	EligibleSubscriptions int     `json:"eligible_subscriptions"`
	Message               string  `json:"message"`
	Error                 string  `json:"error"`
}

type catalogAccount struct {
	ID          int64  `json:"id"`
	Label       string `json:"label"`
	Name        string `json:"name"`
	Status      string `json:"status"`
	Schedulable bool   `json:"schedulable"`
}

type catalogGroup struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
}

type optionCatalog struct {
	Accounts    []catalogAccount `json:"accounts"`
	Groups      []catalogGroup   `json:"groups"`
	GeneratedAt string           `json:"generated_at"`
}

func defaultConfig() Config {
	return Config{
		Enabled:                false,
		DryRun:                 true,
		AccountIDs:             []int64{1},
		TargetGroups:           []string{},
		ResetWeekly:            true,
		MinCycleShiftSeconds:   24 * 60 * 60,
		RearmRemainingSeconds:  5 * 24 * 60 * 60,
		JitterToleranceSeconds: 6 * 60 * 60,
	}
}

func normalizeConfig(raw []byte) ([]byte, Config, error) {
	cfg := defaultConfig()
	if len(bytes.TrimSpace(raw)) == 0 {
		raw = []byte("{}")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, Config{}, fmt.Errorf("解析配置: %w", err)
	}
	_, hasAccountIDs := fields["account_ids"]
	_, hasLegacyAccountID := fields["account_id"]
	if hasAccountIDs && hasLegacyAccountID {
		return nil, Config{}, errors.New("account_ids 与旧字段 account_id 不能同时存在")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil {
		return nil, Config{}, fmt.Errorf("解析配置: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return nil, Config{}, errors.New("配置只能包含一个 JSON 对象")
	}
	if hasLegacyAccountID {
		cfg.AccountIDs = []int64{cfg.LegacyAccountID}
	}
	cfg.LegacyAccountID = 0
	accountSeen := make(map[int64]struct{}, len(cfg.AccountIDs))
	accounts := make([]int64, 0, len(cfg.AccountIDs))
	for _, accountID := range cfg.AccountIDs {
		if accountID <= 0 {
			return nil, Config{}, errors.New("账号 ID 必须是正整数")
		}
		if _, exists := accountSeen[accountID]; exists {
			continue
		}
		accountSeen[accountID] = struct{}{}
		accounts = append(accounts, accountID)
	}
	cfg.AccountIDs = accounts
	if cfg.Enabled && len(cfg.AccountIDs) == 0 {
		return nil, Config{}, errors.New("启用同步前至少选择一个账号")
	}
	seen := make(map[string]struct{}, len(cfg.TargetGroups))
	groups := make([]string, 0, len(cfg.TargetGroups))
	for _, group := range cfg.TargetGroups {
		group = strings.TrimSpace(group)
		if group == "" {
			continue
		}
		key := strings.ToLower(group)
		if _, exists := seen[key]; exists {
			continue
		}
		seen[key] = struct{}{}
		groups = append(groups, group)
	}
	cfg.TargetGroups = groups
	if cfg.Enabled && len(cfg.TargetGroups) == 0 {
		return nil, Config{}, errors.New("启用同步前至少填写一个目标分组名称或 ID")
	}
	if cfg.Enabled && !cfg.ResetDaily && !cfg.ResetWeekly && !cfg.ResetMonthly {
		return nil, Config{}, errors.New("启用同步前至少选择一个重置窗口")
	}
	if cfg.MinCycleShiftSeconds < 24*60*60 || cfg.MinCycleShiftSeconds > 8*24*60*60 {
		return nil, Config{}, errors.New("新周期最小跳变必须在 1 到 8 天之间")
	}
	if cfg.RearmRemainingSeconds < 24*60*60 || cfg.RearmRemainingSeconds > 7*24*60*60 {
		return nil, Config{}, errors.New("新周期最小剩余时间必须在 1 到 7 天之间")
	}
	if cfg.JitterToleranceSeconds < 0 || cfg.JitterToleranceSeconds > 24*60*60 {
		return nil, Config{}, errors.New("时间抖动容差必须在 0 到 24 小时之间")
	}
	if cfg.JitterToleranceSeconds >= cfg.MinCycleShiftSeconds {
		return nil, Config{}, errors.New("时间抖动容差必须小于新周期最小跳变")
	}
	normalized, err := json.Marshal(cfg)
	if err != nil {
		return nil, Config{}, err
	}
	return normalized, cfg, nil
}

type server struct {
	pluginv1.UnimplementedTransportPluginServer
	mu         sync.RWMutex
	configPath string
	config     Config
	transports map[string]*http.Transport
}

func newServer() (*server, error) {
	executable, err := os.Executable()
	if err != nil {
		return nil, fmt.Errorf("读取插件路径: %w", err)
	}
	return &server{
		configPath: filepath.Join(filepath.Dir(executable), configName),
		config:     defaultConfig(),
		transports: make(map[string]*http.Transport),
	}, nil
}

func (s *server) GetInfo(context.Context, *pluginv1.GetInfoRequest) (*pluginv1.GetInfoResponse, error) {
	return &pluginv1.GetInfoResponse{
		PluginId:            pluginID,
		PluginVersion:       pluginVersion,
		ProtocolVersion:     pluginv1.ProtocolVersion,
		TransportApiVersion: pluginv1.TransportAPIVersion,
		Capabilities:        []string{capabilityID},
	}, nil
}

func (s *server) Health(context.Context, *pluginv1.HealthRequest) (*pluginv1.HealthResponse, error) {
	return &pluginv1.HealthResponse{Healthy: true, Message: "配额同步控制面可用"}, nil
}

func (s *server) readCatalog() optionCatalog {
	catalog := optionCatalog{Accounts: []catalogAccount{}, Groups: []catalogGroup{}}
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(s.configPath), catalogName))
	if err == nil {
		_ = json.Unmarshal(raw, &catalog)
	}
	return catalog
}

func (s *server) normalizeForHost(raw []byte) ([]byte, Config, error) {
	_, cfg, err := normalizeConfig(raw)
	if err != nil {
		return nil, Config{}, err
	}
	catalog := s.readCatalog()
	cfg.Catalog = &catalog
	normalized, err := json.Marshal(cfg)
	if err != nil {
		return nil, Config{}, err
	}
	return normalized, cfg, nil
}

func (s *server) ValidateConfig(_ context.Context, request *pluginv1.ValidateConfigRequest) (*pluginv1.ValidateConfigResponse, error) {
	normalized, _, err := s.normalizeForHost(request.GetConfigJson())
	if err != nil {
		return &pluginv1.ValidateConfigResponse{Valid: false, Message: err.Error()}, nil
	}
	return &pluginv1.ValidateConfigResponse{Valid: true, Message: "配置有效", NormalizedConfigJson: normalized}, nil
}

func (s *server) ApplyConfig(_ context.Context, request *pluginv1.ApplyConfigRequest) (*pluginv1.ApplyConfigResponse, error) {
	normalized, cfg, err := s.normalizeForHost(request.GetConfigJson())
	if err != nil {
		return &pluginv1.ApplyConfigResponse{Applied: false, Message: err.Error()}, nil
	}
	if err := atomicWrite(s.configPath, append(normalized, '\n')); err != nil {
		return &pluginv1.ApplyConfigResponse{Applied: false, Message: err.Error()}, nil
	}
	s.mu.Lock()
	s.config = cfg
	s.mu.Unlock()
	return &pluginv1.ApplyConfigResponse{Applied: true, Message: "配置已同步到后台执行器"}, nil
}

func atomicWrite(path string, data []byte) error {
	temp, err := os.CreateTemp(filepath.Dir(path), ".quota-sync-config-*")
	if err != nil {
		return fmt.Errorf("创建配置临时文件: %w", err)
	}
	tempPath := temp.Name()
	defer func() { _ = os.Remove(tempPath) }()
	if err := temp.Chmod(0o600); err != nil {
		_ = temp.Close()
		return err
	}
	if _, err := temp.Write(data); err != nil {
		_ = temp.Close()
		return fmt.Errorf("写入配置: %w", err)
	}
	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return fmt.Errorf("同步配置: %w", err)
	}
	if err := temp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("提交配置: %w", err)
	}
	return nil
}

func (s *server) TestConfig(_ context.Context, request *pluginv1.TestConfigRequest) (*pluginv1.TestConfigResponse, error) {
	_, cfg, err := normalizeConfig(request.GetConfigJson())
	if err != nil {
		return &pluginv1.TestConfigResponse{Success: false, Message: err.Error()}, nil
	}
	_, err = os.Stat(s.configPath)
	if err != nil {
		return &pluginv1.TestConfigResponse{Success: false, Message: "配置尚未落盘，请先保存"}, nil
	}
	statusRaw, err := os.ReadFile(filepath.Join(filepath.Dir(s.configPath), "quota-sync-status.json"))
	if err != nil {
		message := fmt.Sprintf("配置已保存：账号 %d 个，目标分组 %d 个；等待后台执行器校验", len(cfg.AccountIDs), len(cfg.TargetGroups))
		return &pluginv1.TestConfigResponse{Success: true, Message: message}, nil
	}
	var status executorStatus
	cfg.Catalog = nil
	businessConfig, marshalErr := json.Marshal(cfg)
	if marshalErr != nil {
		return &pluginv1.TestConfigResponse{Success: false, Message: marshalErr.Error()}, nil
	}
	digest := fmt.Sprintf("%x", sha256.Sum256(businessConfig))
	if err := json.Unmarshal(statusRaw, &status); err != nil || status.ConfigSHA256 != digest {
		return &pluginv1.TestConfigResponse{Success: true, Message: "配置已保存；等待后台执行器读取最新版本"}, nil
	}
	if !status.OK {
		message := strings.TrimSpace(status.Error)
		if message == "" {
			message = "后台执行器校验失败"
		}
		return &pluginv1.TestConfigResponse{Success: false, Message: message}, nil
	}
	if !status.Enabled {
		return &pluginv1.TestConfigResponse{Success: true, Message: "后台执行器已读取配置：业务开关关闭"}, nil
	}
	message := fmt.Sprintf("真实校验通过：账号 ID %v，分组 ID %v，有效订阅 %d 个，演练=%t", status.SelectedAccountIDs, status.ResolvedGroupIDs, status.EligibleSubscriptions, status.DryRun)
	return &pluginv1.TestConfigResponse{Success: true, Message: message}, nil
}

func (s *server) transportFor(proxyURL string) (*http.Transport, error) {
	proxyURL = strings.TrimSpace(proxyURL)
	s.mu.RLock()
	transport := s.transports[proxyURL]
	s.mu.RUnlock()
	if transport != nil {
		return transport, nil
	}

	created := http.DefaultTransport.(*http.Transport).Clone()
	created.ForceAttemptHTTP2 = true
	if proxyURL != "" {
		parsed, err := url.Parse(proxyURL)
		if err != nil || parsed.Scheme == "" || parsed.Host == "" {
			return nil, errors.New("代理地址无效")
		}
		created.Proxy = http.ProxyURL(parsed)
	}
	s.mu.Lock()
	if existing := s.transports[proxyURL]; existing != nil {
		created.CloseIdleConnections()
		transport = existing
	} else {
		s.transports[proxyURL] = created
		transport = created
	}
	s.mu.Unlock()
	return transport, nil
}

func sendForwardError(stream grpc.BidiStreamingServer[pluginv1.ForwardRequest, pluginv1.ForwardResponse], code, message string, requestSent bool) error {
	return stream.Send(&pluginv1.ForwardResponse{Frame: &pluginv1.ForwardResponse_Error{Error: &pluginv1.ForwardResponseError{
		Code: code, Message: message, RequestSent: requestSent,
	}}})
}

func (s *server) Forward(stream grpc.BidiStreamingServer[pluginv1.ForwardRequest, pluginv1.ForwardResponse]) error {
	startedAt := time.Now()
	first, err := stream.Recv()
	if err != nil {
		return sendForwardError(stream, "INVALID_REQUEST_STREAM", "未收到请求起始帧", false)
	}
	start := first.GetStart()
	if start == nil {
		return sendForwardError(stream, "INVALID_REQUEST_STREAM", "首帧必须是请求起始帧", false)
	}
	parsedURL, err := url.Parse(start.GetUrl())
	if err != nil || (parsedURL.Scheme != "http" && parsedURL.Scheme != "https") || parsedURL.Host == "" {
		return sendForwardError(stream, "INVALID_UPSTREAM_URL", "上游地址无效", false)
	}
	transport, err := s.transportFor(start.GetProxyUrl())
	if err != nil {
		return sendForwardError(stream, "INVALID_PROXY_URL", err.Error(), false)
	}

	var bodyReader io.Reader
	var pipeReader *io.PipeReader
	var pipeWriter *io.PipeWriter
	if start.GetHasBody() {
		pipeReader, pipeWriter = io.Pipe()
		bodyReader = pipeReader
	}
	request, err := http.NewRequestWithContext(stream.Context(), start.GetMethod(), parsedURL.String(), bodyReader)
	if err != nil {
		return sendForwardError(stream, "INVALID_UPSTREAM_REQUEST", "无法创建上游请求", false)
	}
	request.Host = start.GetHost()
	for name, values := range start.GetHeaders() {
		for _, value := range values.GetValues() {
			request.Header.Add(name, value)
		}
	}
	if start.GetContentLength() >= 0 {
		request.ContentLength = start.GetContentLength()
	}

	type roundTripResult struct {
		response *http.Response
		err      error
	}
	resultCh := make(chan roundTripResult, 1)
	go func() {
		response, requestErr := (&http.Client{Transport: transport}).Do(request)
		resultCh <- roundTripResult{response: response, err: requestErr}
	}()

	bodyEnded := false
	for !bodyEnded {
		frame, recvErr := stream.Recv()
		if recvErr != nil {
			if pipeWriter != nil {
				_ = pipeWriter.CloseWithError(recvErr)
			}
			return sendForwardError(stream, "REQUEST_STREAM_READ_FAILED", "读取请求体失败", true)
		}
		if frame.GetBodyEnd() {
			bodyEnded = true
			if pipeWriter != nil {
				_ = pipeWriter.Close()
			}
			continue
		}
		chunk := frame.GetBodyChunk()
		if chunk == nil || pipeWriter == nil {
			if pipeWriter != nil {
				_ = pipeWriter.CloseWithError(errors.New("请求帧顺序无效"))
			}
			return sendForwardError(stream, "INVALID_REQUEST_STREAM", "请求帧顺序无效", true)
		}
		if _, writeErr := pipeWriter.Write(chunk); writeErr != nil {
			return sendForwardError(stream, "UPSTREAM_UPLOAD_FAILED", "向上游传输请求体失败", true)
		}
	}

	var result roundTripResult
	select {
	case result = <-resultCh:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	if pipeReader != nil {
		_ = pipeReader.Close()
	}
	if result.err != nil {
		return sendForwardError(stream, "UPSTREAM_TRANSPORT_ERROR", "上游 HTTP 传输失败", true)
	}
	defer result.response.Body.Close()

	headers := make(map[string]*pluginv1.HeaderValues, len(result.response.Header))
	for name, values := range result.response.Header {
		headers[name] = &pluginv1.HeaderValues{Values: append([]string(nil), values...)}
	}
	if err := stream.Send(&pluginv1.ForwardResponse{Frame: &pluginv1.ForwardResponse_Start{Start: &pluginv1.ForwardResponseStart{
		StatusCode:    int32(result.response.StatusCode),
		Status:        result.response.Status,
		Protocol:      result.response.Proto,
		ProtocolMajor: int32(result.response.ProtoMajor),
		ProtocolMinor: int32(result.response.ProtoMinor),
		Headers:       headers,
		ContentLength: result.response.ContentLength,
	}}}); err != nil {
		return err
	}

	buffer := make([]byte, 32*1024)
	var bytesReceived int64
	for {
		read, readErr := result.response.Body.Read(buffer)
		if read > 0 {
			bytesReceived += int64(read)
			chunk := append([]byte(nil), buffer[:read]...)
			if err := stream.Send(&pluginv1.ForwardResponse{Frame: &pluginv1.ForwardResponse_BodyChunk{BodyChunk: chunk}}); err != nil {
				return err
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return sendForwardError(stream, "UPSTREAM_RESPONSE_READ_FAILED", "读取上游响应失败", true)
		}
	}
	return stream.Send(&pluginv1.ForwardResponse{Frame: &pluginv1.ForwardResponse_End{End: &pluginv1.ForwardResponseEnd{
		BytesReceived: bytesReceived,
		DurationMs:    time.Since(startedAt).Milliseconds(),
	}}})
}

func main() {
	srv, err := newServer()
	if err != nil {
		panic(err)
	}
	pluginv1.Serve(srv)
}

var _ pluginv1.TransportPluginServer = (*server)(nil)
