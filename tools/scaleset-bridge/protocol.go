package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"net/url"
	"runtime/debug"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/actions/scaleset"
	"github.com/hashicorp/go-retryablehttp"
)

const protocolVersion = 3

// An installation token is minted by the App key's home host and pushed with roughly an hour of
// life. A session starts only with comfortable headroom, and a poll (the session's only idle
// boundary) refuses once the token is near expiry so the Rust supervisor restarts the bridge with a
// fresh read of the pushed token file. Operations after an outstanding poll still complete.
const minInstallationTokenStartLife = 15 * time.Minute
const installationTokenPollMargin = 10 * time.Minute
const maxInstallationTokenBytes = 1024
const bridgeVersion = "0.1.0"
const maxServiceResponseBytes = 2 * 1024 * 1024
const syntheticRunnerRequestMarker int64 = 1 << 62
const syntheticRunnerRequestDomain = "smolrunner.scale-set.direct-assignment.v1"

type protocolRequest struct {
	Version          int         `json:"version"`
	Operation        string      `json:"operation"`
	Start            startConfig `json:"start,omitempty"`
	MessageID        int         `json:"message_id,omitempty"`
	LastAckedID      int         `json:"last_acked_message_id,omitempty"`
	MaxCapacity      int         `json:"max_capacity,omitempty"`
	RunnerRequestIDs []int64     `json:"runner_request_ids,omitempty"`
	RunnerName       string      `json:"runner_name,omitempty"`
	RunnerID         int64       `json:"runner_id,omitempty"`
	WorkFolder       string      `json:"work_folder,omitempty"`
}

type startConfig struct {
	GitHubConfigURL string `json:"github_config_url"`
	ClientID        string `json:"client_id,omitempty"`
	InstallationID  int64  `json:"installation_id,omitempty"`
	PrivateKey      string `json:"private_key,omitempty"`
	// InstallationToken is the alternative to App key authentication: a GitHub App installation
	// token that the key's home host minted. InstallationTokenExpiresAt is its Unix expiry second.
	InstallationToken          string   `json:"installation_token,omitempty"`
	InstallationTokenExpiresAt int64    `json:"installation_token_expires_at,omitempty"`
	ScaleSetID                 int      `json:"scale_set_id"`
	ScaleSetName               string   `json:"scale_set_name"`
	RunnerGroupID              int      `json:"runner_group_id"`
	Labels                     []string `json:"labels"`
	Owner                      string   `json:"owner"`
	MaxCapacity                int      `json:"max_capacity"`
}

type protocolResponse struct {
	Version          int         `json:"version"`
	Type             string      `json:"type"`
	Code             string      `json:"code,omitempty"`
	ScaleSetID       int         `json:"scale_set_id,omitempty"`
	MessageID        int         `json:"message_id,omitempty"`
	Statistics       *statistics `json:"statistics,omitempty"`
	Events           []jobEvent  `json:"events,omitempty"`
	AcquiredRequests []int64     `json:"acquired_requests,omitempty"`
	Runner           *runner     `json:"runner,omitempty"`
	EncodedJITConfig string      `json:"encoded_jit_config,omitempty"`
}

type statistics struct {
	AvailableJobs     int `json:"available_jobs"`
	AcquiredJobs      int `json:"acquired_jobs"`
	AssignedJobs      int `json:"assigned_jobs"`
	RunningJobs       int `json:"running_jobs"`
	RegisteredRunners int `json:"registered_runners"`
	BusyRunners       int `json:"busy_runners"`
	IdleRunners       int `json:"idle_runners"`
}

type jobEvent struct {
	Kind            string   `json:"kind"`
	RunnerRequestID int64    `json:"runner_request_id"`
	Repository      string   `json:"repository"`
	Owner           string   `json:"owner"`
	JobID           string   `json:"job_id"`
	WorkflowRunID   int64    `json:"workflow_run_id"`
	RequestLabels   []string `json:"request_labels"`
	RunnerID        int      `json:"runner_id,omitempty"`
	RunnerName      string   `json:"runner_name,omitempty"`
	Result          string   `json:"result,omitempty"`
}

type runner struct {
	ID         int    `json:"id"`
	Name       string `json:"name"`
	ScaleSetID int    `json:"scale_set_id"`
}

func errorResponse(code string) protocolResponse {
	return protocolResponse{Version: protocolVersion, Type: "error", Code: code}
}

func (config startConfig) validate() error {
	parsed, err := url.Parse(config.GitHubConfigURL)
	if err != nil || parsed.Scheme != "https" || !strings.EqualFold(parsed.Host, "github.com") || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("invalid GitHub configuration URL")
	}
	parts := strings.Split(strings.Trim(parsed.Path, "/"), "/")
	if len(parts) < 1 || len(parts) > 2 {
		return errors.New("unsupported GitHub configuration scope")
	}
	for _, part := range parts {
		if !boundedToken(part, 100) {
			return errors.New("invalid GitHub configuration scope")
		}
	}
	if config.usesInstallationToken() {
		// Exactly one credential kind: a token start carries no App identity or key.
		if config.ClientID != "" || config.InstallationID != 0 || config.PrivateKey != "" {
			return errors.New("ambiguous GitHub credential")
		}
		if !boundedToken(config.InstallationToken, maxInstallationTokenBytes) || config.InstallationTokenExpiresAt <= 0 {
			return errors.New("invalid GitHub installation token")
		}
	} else {
		if !boundedToken(config.ClientID, 100) || config.InstallationID <= 0 {
			return errors.New("invalid GitHub App identity")
		}
		if len(config.PrivateKey) == 0 || len(config.PrivateKey) > 64*1024 || !utf8.ValidString(config.PrivateKey) {
			return errors.New("invalid GitHub App private key")
		}
	}
	if config.ScaleSetID <= 0 || config.RunnerGroupID <= 0 || config.MaxCapacity < 0 || config.MaxCapacity > 64 {
		return errors.New("invalid scale set bounds")
	}
	if !boundedToken(config.ScaleSetName, 100) || !boundedToken(config.Owner, 100) {
		return errors.New("invalid scale set identity")
	}
	if len(config.Labels) == 0 || len(config.Labels) > 8 {
		return errors.New("invalid scale set labels")
	}
	seen := make(map[string]struct{}, len(config.Labels))
	for _, label := range config.Labels {
		if !boundedToken(label, 100) {
			return errors.New("invalid scale set label")
		}
		if _, exists := seen[label]; exists {
			return errors.New("duplicate scale set label")
		}
		seen[label] = struct{}{}
	}
	return nil
}

func (config startConfig) usesInstallationToken() bool {
	return config.InstallationToken != "" || config.InstallationTokenExpiresAt != 0
}

// clearSecrets drops every credential the start request carried.
func (config *startConfig) clearSecrets() {
	config.PrivateKey = ""
	config.InstallationToken = ""
}

func boundedToken(value string, maximum int) bool {
	if len(value) == 0 || len(value) > maximum || strings.TrimSpace(value) != value {
		return false
	}
	for _, character := range value {
		if character < 0x21 || character > 0x7e {
			return false
		}
	}
	return true
}

type sessionAPI interface {
	GetMessage(context.Context, int, int) (*scaleset.RunnerScaleSetMessage, error)
	DeleteMessage(context.Context, int) error
	AcquireJobs(context.Context, []int64) ([]int64, error)
	Session() scaleset.RunnerScaleSetSession
	Close(context.Context) error
}

type backend interface {
	Poll(context.Context, int, int) (*scaleset.RunnerScaleSetMessage, error)
	DeleteMessage(context.Context, int) error
	AcquireJobs(context.Context, []int64) ([]int64, error)
	GenerateJIT(context.Context, string, string) (*scaleset.RunnerScaleSetJitRunnerConfig, error)
	GetRunner(context.Context, int) (*scaleset.RunnerReference, error)
	GetRunnerByName(context.Context, string) (*scaleset.RunnerReference, error)
	RemoveRunner(context.Context, int64) error
	ValidateScaleSet(context.Context) error
	InitialStatistics() *scaleset.RunnerScaleSetStatistic
	Close(context.Context) error
}

type backendFactory func(context.Context, startConfig) (backend, error)

type officialBackend struct {
	client   *scaleset.Client
	session  sessionAPI
	setID    int
	expected startConfig
}

func newOfficialBackend(ctx context.Context, config startConfig) (backend, error) {
	if err := config.validate(); err != nil {
		return nil, err
	}
	client, err := newOfficialClient(config, newStrictHTTPClient())
	if err != nil {
		return nil, err
	}
	set, err := client.GetRunnerScaleSetByID(ctx, config.ScaleSetID)
	if err != nil {
		return nil, err
	}
	if err := validateScaleSet(set, config); err != nil {
		return nil, err
	}
	session, err := client.MessageSessionClient(ctx, set.ID, config.Owner)
	if err != nil {
		return nil, err
	}
	expected := config
	expected.clearSecrets()
	return &officialBackend{client: client, session: session, setID: set.ID, expected: expected}, nil
}

// newOfficialClient builds the pinned client with exactly the credential kind the start carried. The
// pinned client sends an installation token exactly as it sends a personal access token: as the
// bearer for the registration-token exchange that opens the Actions service session.
func newOfficialClient(config startConfig, strictHTTP *retryablehttp.Client) (*scaleset.Client, error) {
	systemInfo := scaleset.SystemInfo{
		System:     "smolrunner",
		Version:    bridgeVersion,
		CommitSHA:  buildCommit(),
		ScaleSetID: config.ScaleSetID,
		Subsystem:  "bridge",
	}
	if config.usesInstallationToken() {
		return scaleset.NewClientWithPersonalAccessToken(scaleset.NewClientWithPersonalAccessTokenConfig{
			GitHubConfigURL:     config.GitHubConfigURL,
			PersonalAccessToken: config.InstallationToken,
			SystemInfo:          systemInfo,
		}, scaleset.WithRetryableHTTPClint(strictHTTP))
	}
	return scaleset.NewClientWithGitHubApp(scaleset.ClientWithGitHubAppConfig{
		GitHubConfigURL: config.GitHubConfigURL,
		GitHubAppAuth: scaleset.GitHubAppAuth{
			ClientID:       config.ClientID,
			InstallationID: config.InstallationID,
			PrivateKey:     config.PrivateKey,
		},
		SystemInfo: systemInfo,
	}, scaleset.WithRetryableHTTPClint(strictHTTP))
}

func newStrictHTTPClient() *retryablehttp.Client {
	client := retryablehttp.NewClient()
	client.RetryMax = 0
	client.HTTPClient.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	basePolicy := client.CheckRetry
	client.CheckRetry = func(ctx context.Context, response *http.Response, requestErr error) (bool, error) {
		retry, policyErr := basePolicy(ctx, response, requestErr)
		if retry || policyErr != nil || requestErr != nil || response == nil || response.Request == nil || response.StatusCode != http.StatusOK || response.Request.Header.Get(scaleset.HeaderScaleSetMaxCapacity) == "" {
			return retry, policyErr
		}
		if err := validateScaleSetMessageHTTPResponse(response); err != nil {
			return false, err
		}
		return false, nil
	}
	return client
}

func validateScaleSetMessageHTTPResponse(response *http.Response) error {
	body, err := io.ReadAll(io.LimitReader(response.Body, maxServiceResponseBytes+1))
	if err != nil {
		return errors.New("failed to read bounded scale set message")
	}
	if len(body) > maxServiceResponseBytes {
		return errors.New("scale set message exceeds bound")
	}
	if err := response.Body.Close(); err != nil {
		return errors.New("failed to close scale set message body")
	}
	response.Body = io.NopCloser(bytes.NewReader(body))
	return validateScaleSetMessageBytes(body)
}

type strictMessageEnvelope struct {
	MessageID   int                               `json:"messageId"`
	MessageType string                            `json:"messageType"`
	Body        string                            `json:"body"`
	Statistics  *scaleset.RunnerScaleSetStatistic `json:"statistics"`
}

func validateScaleSetMessageBytes(body []byte) error {
	var envelope strictMessageEnvelope
	if err := strictJSON(body, &envelope); err != nil || envelope.MessageType != "RunnerScaleSetJobMessages" || envelope.MessageID <= 0 || envelope.Statistics == nil {
		return errors.New("invalid scale set message envelope")
	}
	var messages []json.RawMessage
	if envelope.Body != "" {
		if err := strictJSON([]byte(envelope.Body), &messages); err != nil || len(messages) > 50 {
			return errors.New("invalid scale set message body")
		}
	}
	for _, message := range messages {
		var fields map[string]json.RawMessage
		if err := strictJSON(message, &fields); err != nil {
			return errors.New("invalid scale set job message")
		}
		rawType, exists := fields["messageType"]
		if !exists {
			return errors.New("missing scale set job message type")
		}
		var messageType scaleset.MessageType
		if err := strictJSON(rawType, &messageType); err != nil {
			return errors.New("invalid scale set job message type")
		}
		var target any
		switch messageType {
		case scaleset.MessageTypeJobAvailable:
			target = &scaleset.JobAvailable{}
		case scaleset.MessageTypeJobAssigned:
			target = &scaleset.JobAssigned{}
		case scaleset.MessageTypeJobStarted:
			target = &scaleset.JobStarted{}
		case scaleset.MessageTypeJobCompleted:
			target = &scaleset.JobCompleted{}
		default:
			return errors.New("unsupported scale set job message type")
		}
		if err := strictJSON(message, target); err != nil {
			return errors.New("invalid scale set job message shape")
		}
	}
	return nil
}

func strictJSON(input []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(input))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("trailing JSON value")
	}
	return nil
}

func buildCommit() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "development"
	}
	var revision string
	modified := false
	for _, setting := range info.Settings {
		switch setting.Key {
		case "vcs.revision":
			revision = setting.Value
		case "vcs.modified":
			modified = setting.Value == "true"
		}
	}
	if modified || len(revision) != 40 {
		return "development"
	}
	for _, character := range revision {
		if !((character >= '0' && character <= '9') || (character >= 'a' && character <= 'f')) {
			return "development"
		}
	}
	return revision
}

func validateScaleSet(set *scaleset.RunnerScaleSet, config startConfig) error {
	if set == nil || set.ID != config.ScaleSetID || set.Name != config.ScaleSetName || set.RunnerGroupID != config.RunnerGroupID || !set.RunnerSetting.DisableUpdate {
		return errors.New("scale set identity mismatch")
	}
	labels := make([]string, 0, len(set.Labels))
	for _, label := range set.Labels {
		labels = append(labels, label.Name)
	}
	expected := append([]string(nil), config.Labels...)
	slices.Sort(labels)
	slices.Sort(expected)
	if !slices.Equal(labels, expected) {
		return errors.New("scale set label mismatch")
	}
	return nil
}

func (backend *officialBackend) Poll(ctx context.Context, lastMessageID, maxCapacity int) (*scaleset.RunnerScaleSetMessage, error) {
	return backend.session.GetMessage(ctx, lastMessageID, maxCapacity)
}
func (backend *officialBackend) DeleteMessage(ctx context.Context, messageID int) error {
	return backend.session.DeleteMessage(ctx, messageID)
}
func (backend *officialBackend) AcquireJobs(ctx context.Context, requestIDs []int64) ([]int64, error) {
	return backend.session.AcquireJobs(ctx, requestIDs)
}
func (backend *officialBackend) GenerateJIT(ctx context.Context, name, workFolder string) (*scaleset.RunnerScaleSetJitRunnerConfig, error) {
	return backend.client.GenerateJitRunnerConfig(ctx, &scaleset.RunnerScaleSetJitRunnerSetting{Name: name, WorkFolder: workFolder}, backend.setID)
}
func (backend *officialBackend) GetRunner(ctx context.Context, id int) (*scaleset.RunnerReference, error) {
	return backend.client.GetRunner(ctx, id)
}
func (backend *officialBackend) GetRunnerByName(ctx context.Context, name string) (*scaleset.RunnerReference, error) {
	return backend.client.GetRunnerByName(ctx, name)
}
func (backend *officialBackend) RemoveRunner(ctx context.Context, id int64) error {
	return backend.client.RemoveRunner(ctx, id)
}
func (backend *officialBackend) ValidateScaleSet(ctx context.Context) error {
	set, err := backend.client.GetRunnerScaleSetByID(ctx, backend.setID)
	if err != nil {
		return err
	}
	return validateScaleSet(set, backend.expected)
}
func (backend *officialBackend) InitialStatistics() *scaleset.RunnerScaleSetStatistic {
	return backend.session.Session().Statistics
}
func (backend *officialBackend) Close(ctx context.Context) error { return backend.session.Close(ctx) }

type pendingMessage struct {
	messageID int
	available []int64
	deleted   bool
}

type server struct {
	factory backendFactory
	now     func() time.Time
	// credentialExpiresAt is zero for App key sessions, whose client refreshes its own tokens.
	credentialExpiresAt time.Time
	backend             backend
	config              startConfig
	lastStatistics      statistics
	lastAckedID         int
	pending             *pendingMessage
	cursorSet           bool
}

func newServer(factory backendFactory) *server { return &server{factory: factory, now: time.Now} }

func (server *server) close() {
	if server.backend != nil {
		_ = server.backend.Close(context.Background())
	}
}

func (server *server) handle(ctx context.Context, request protocolRequest) protocolResponse {
	switch request.Operation {
	case "start":
		return server.start(ctx, request.Start)
	case "poll":
		return server.poll(ctx, request.MaxCapacity)
	case "resume":
		return server.resume(request.LastAckedID)
	case "ack":
		return server.ack(ctx, request.MessageID)
	case "acquire":
		return server.acquire(ctx, request.RunnerRequestIDs)
	case "generate_jit":
		return server.generateJIT(ctx, request.RunnerName, request.WorkFolder)
	case "observe_runner":
		return server.observeRunner(ctx, request.RunnerName)
	case "remove_runner":
		return server.removeRunner(ctx, request.RunnerID, request.RunnerName)
	default:
		return errorResponse("unsupported_operation")
	}
}

// resume restores only the durable acknowledged-message cursor in a fresh bridge process. It is
// intentionally a one-shot, pre-poll operation: Rust owns the durable cursor and uses zero-capacity
// polling after an ambiguous acquisition so later lifecycle evidence cannot admit another job.
func (server *server) resume(lastAckedID int) protocolResponse {
	if server.backend == nil {
		return errorResponse("not_started")
	}
	if server.cursorSet || server.pending != nil || server.lastAckedID != 0 || lastAckedID <= 0 {
		return errorResponse("invalid_recovery")
	}
	server.lastAckedID = lastAckedID
	server.cursorSet = true
	return protocolResponse{Version: protocolVersion, Type: "restored", MessageID: lastAckedID}
}

func (server *server) start(ctx context.Context, config startConfig) protocolResponse {
	if server.backend != nil {
		return errorResponse("already_started")
	}
	if err := config.validate(); err != nil {
		return errorResponse("invalid_start")
	}
	var expiresAt time.Time
	if config.usesInstallationToken() {
		expiresAt = time.Unix(config.InstallationTokenExpiresAt, 0)
		if expiresAt.Sub(server.now()) < minInstallationTokenStartLife {
			return errorResponse("credential_expiring")
		}
	}
	backend, err := server.factory(ctx, config)
	if err != nil {
		return errorResponse("start_failed")
	}
	initial, err := normalizeStatistics(backend.InitialStatistics())
	if err != nil || initial == nil {
		_ = backend.Close(context.Background())
		return errorResponse("start_failed")
	}
	server.backend = backend
	server.config = config
	server.config.clearSecrets()
	server.credentialExpiresAt = expiresAt
	server.lastStatistics = *initial
	return protocolResponse{Version: protocolVersion, Type: "ready", ScaleSetID: config.ScaleSetID, Statistics: initial}
}

func (server *server) poll(ctx context.Context, maxCapacity int) protocolResponse {
	if server.backend == nil {
		return errorResponse("not_started")
	}
	if maxCapacity < 0 || maxCapacity > server.config.MaxCapacity {
		return errorResponse("invalid_capacity")
	}
	if server.pending != nil {
		return errorResponse("ack_required")
	}
	if !server.credentialExpiresAt.IsZero() && server.credentialExpiresAt.Sub(server.now()) < installationTokenPollMargin {
		// End the session at its idle boundary, before any service call, so the supervisor
		// restarts the bridge with a freshly pushed token.
		return errorResponse("credential_expiring")
	}
	server.cursorSet = true
	message, err := server.backend.Poll(ctx, server.lastAckedID, maxCapacity)
	if err != nil {
		return errorResponse("poll_failed")
	}
	if message == nil {
		current := server.lastStatistics
		return protocolResponse{Version: protocolVersion, Type: "idle", Statistics: &current}
	}
	if message.MessageID <= server.lastAckedID || message.Statistics == nil {
		return errorResponse("invalid_message")
	}
	response, available, normalizeErr := normalizeMessage(message)
	if normalizeErr != nil || len(available) > maxCapacity {
		return errorResponse("invalid_message")
	}
	if !responseFitsProtocolLine(response) {
		return errorResponse("invalid_message")
	}
	server.lastStatistics = *response.Statistics
	server.pending = &pendingMessage{messageID: message.MessageID, available: available}
	return response
}

func (server *server) ack(ctx context.Context, messageID int) protocolResponse {
	if server.backend == nil {
		return errorResponse("not_started")
	}
	if server.pending == nil || messageID != server.pending.messageID {
		return errorResponse("message_mismatch")
	}
	if !server.pending.deleted {
		if err := server.backend.DeleteMessage(ctx, messageID); err != nil {
			return errorResponse("ack_failed")
		}
		server.pending.deleted = true
	}
	var acquired []int64
	if len(server.pending.available) > 0 {
		var err error
		acquired, err = server.backend.AcquireJobs(ctx, server.pending.available)
		if err != nil {
			return errorResponse("acquire_failed")
		}
		if err := validateAcquiredJobs(server.pending.available, acquired); err != nil {
			return errorResponse("invalid_acquisition")
		}
		slices.Sort(acquired)
	}
	server.lastAckedID = messageID
	server.pending = nil
	return protocolResponse{Version: protocolVersion, Type: "acked", MessageID: messageID, AcquiredRequests: acquired}
}

func (server *server) acquire(ctx context.Context, requestIDs []int64) protocolResponse {
	if server.backend == nil {
		return errorResponse("not_started")
	}
	if server.pending != nil {
		return errorResponse("ack_required")
	}
	if len(requestIDs) == 0 || len(requestIDs) > 50 {
		return errorResponse("invalid_acquisition_request")
	}
	if err := validateAcquiredJobs(requestIDs, nil); err != nil {
		return errorResponse("invalid_acquisition_request")
	}
	acquired, err := server.backend.AcquireJobs(ctx, requestIDs)
	if err != nil {
		return errorResponse("acquire_failed")
	}
	if err := validateAcquiredJobs(requestIDs, acquired); err != nil {
		return errorResponse("invalid_acquisition")
	}
	slices.Sort(acquired)
	return protocolResponse{Version: protocolVersion, Type: "acquired", AcquiredRequests: acquired}
}

func validateAcquiredJobs(available, acquired []int64) error {
	expected := make(map[int64]struct{}, len(available))
	for _, id := range available {
		if id <= 0 {
			return errors.New("invalid available job request")
		}
		if _, exists := expected[id]; exists {
			return errors.New("duplicate available job request")
		}
		expected[id] = struct{}{}
	}
	if len(acquired) > len(expected) {
		return errors.New("too many acquired job requests")
	}
	seen := make(map[int64]struct{}, len(acquired))
	for _, id := range acquired {
		if id <= 0 {
			return errors.New("invalid acquired job request")
		}
		if _, exists := expected[id]; !exists {
			return errors.New("foreign acquired job request")
		}
		if _, exists := seen[id]; exists {
			return errors.New("duplicate acquired job request")
		}
		seen[id] = struct{}{}
	}
	return nil
}

func (server *server) generateJIT(ctx context.Context, name, workFolder string) protocolResponse {
	if server.backend == nil {
		return errorResponse("not_started")
	}
	if !boundedToken(name, 100) || (workFolder != "" && !boundedToken(workFolder, 100)) {
		return errorResponse("invalid_runner")
	}
	if err := server.backend.ValidateScaleSet(ctx); err != nil {
		return errorResponse("scale_set_drift")
	}
	result, err := server.backend.GenerateJIT(ctx, name, workFolder)
	if err != nil || result == nil || result.Runner == nil || result.Runner.ID <= 0 || result.Runner.Name != name || result.Runner.RunnerScaleSetID != server.config.ScaleSetID || result.EncodedJITConfig == "" || len(result.EncodedJITConfig) > 64*1024 {
		return errorResponse("jit_failed")
	}
	if err := server.backend.ValidateScaleSet(ctx); err != nil {
		return errorResponse("scale_set_drift")
	}
	return protocolResponse{Version: protocolVersion, Type: "jit", Runner: normalizeRunner(result.Runner), EncodedJITConfig: result.EncodedJITConfig}
}

func (server *server) observeRunner(ctx context.Context, name string) protocolResponse {
	if server.backend == nil || !boundedToken(name, 100) {
		return errorResponse("invalid_runner")
	}
	result, err := server.backend.GetRunnerByName(ctx, name)
	if err != nil {
		return errorResponse("runner_unavailable")
	}
	if result == nil {
		return protocolResponse{Version: protocolVersion, Type: "runner_absent"}
	}
	if result.ID <= 0 || result.Name != name || result.RunnerScaleSetID != server.config.ScaleSetID {
		return errorResponse("runner_unavailable")
	}
	return protocolResponse{Version: protocolVersion, Type: "runner", Runner: normalizeRunner(result)}
}

func (server *server) removeRunner(ctx context.Context, id int64, name string) protocolResponse {
	if server.backend == nil || id <= 0 || id > math.MaxInt32 || !boundedToken(name, 100) {
		return errorResponse("invalid_runner")
	}
	current, err := server.backend.GetRunner(ctx, int(id))
	if err != nil || current == nil || current.ID != int(id) || current.Name != name || current.RunnerScaleSetID != server.config.ScaleSetID {
		return errorResponse("runner_unavailable")
	}
	if err := server.backend.RemoveRunner(ctx, id); err != nil {
		return errorResponse("remove_failed")
	}
	return protocolResponse{Version: protocolVersion, Type: "removed", Runner: normalizeRunner(current)}
}

func normalizeStatistics(input *scaleset.RunnerScaleSetStatistic) (*statistics, error) {
	if input == nil {
		return nil, nil
	}
	values := []int{input.TotalAvailableJobs, input.TotalAcquiredJobs, input.TotalAssignedJobs, input.TotalRunningJobs, input.TotalRegisteredRunners, input.TotalBusyRunners, input.TotalIdleRunners}
	for _, value := range values {
		if value < 0 || value > math.MaxInt32 {
			return nil, errors.New("invalid scale set statistic")
		}
	}
	if input.TotalRunningJobs > input.TotalAssignedJobs || input.TotalBusyRunners > input.TotalRegisteredRunners || input.TotalIdleRunners > input.TotalRegisteredRunners {
		return nil, errors.New("inconsistent scale set statistic")
	}
	if input.TotalBusyRunners > math.MaxInt-input.TotalIdleRunners || input.TotalBusyRunners+input.TotalIdleRunners > input.TotalRegisteredRunners {
		return nil, errors.New("inconsistent runner statistic")
	}
	return &statistics{AvailableJobs: input.TotalAvailableJobs, AcquiredJobs: input.TotalAcquiredJobs, AssignedJobs: input.TotalAssignedJobs, RunningJobs: input.TotalRunningJobs, RegisteredRunners: input.TotalRegisteredRunners, BusyRunners: input.TotalBusyRunners, IdleRunners: input.TotalIdleRunners}, nil
}

func normalizeMessage(message *scaleset.RunnerScaleSetMessage) (protocolResponse, []int64, error) {
	statistics, err := normalizeStatistics(message.Statistics)
	if err != nil || statistics == nil {
		return protocolResponse{}, nil, errors.New("invalid message statistics")
	}
	eventCount := len(message.JobAvailableMessages) + len(message.JobAssignedMessages) + len(message.JobStartedMessages) + len(message.JobCompletedMessages)
	events := make([]jobEvent, 0, eventCount)
	if eventCount > 50 {
		return protocolResponse{}, nil, errors.New("too many message events")
	}
	available := make([]int64, 0, len(message.JobAvailableMessages))
	seenAvailable := make(map[int64]struct{}, len(message.JobAvailableMessages))
	for _, input := range message.JobAvailableMessages {
		if input == nil {
			return protocolResponse{}, nil, errors.New("nil available event")
		}
		event, eventErr := normalizeJob("available", input.JobMessageBase, 0, "", "")
		if eventErr != nil {
			return protocolResponse{}, nil, eventErr
		}
		if _, exists := seenAvailable[input.RunnerRequestID]; exists {
			return protocolResponse{}, nil, errors.New("duplicate available job request")
		}
		seenAvailable[input.RunnerRequestID] = struct{}{}
		events = append(events, event)
		available = append(available, input.RunnerRequestID)
	}
	for _, input := range message.JobAssignedMessages {
		if input == nil {
			return protocolResponse{}, nil, errors.New("nil assigned event")
		}
		event, eventErr := normalizeJob("assigned", input.JobMessageBase, 0, "", "")
		if eventErr != nil {
			return protocolResponse{}, nil, eventErr
		}
		events = append(events, event)
	}
	for _, input := range message.JobStartedMessages {
		if input == nil {
			return protocolResponse{}, nil, errors.New("nil started event")
		}
		event, eventErr := normalizeJob("started", input.JobMessageBase, input.RunnerID, input.RunnerName, "")
		if eventErr != nil {
			return protocolResponse{}, nil, eventErr
		}
		events = append(events, event)
	}
	for _, input := range message.JobCompletedMessages {
		if input == nil {
			return protocolResponse{}, nil, errors.New("nil completed event")
		}
		event, eventErr := normalizeJob("completed", input.JobMessageBase, input.RunnerID, input.RunnerName, input.Result)
		if eventErr != nil {
			return protocolResponse{}, nil, eventErr
		}
		events = append(events, event)
	}
	return protocolResponse{Version: protocolVersion, Type: "message", MessageID: message.MessageID, Statistics: statistics, Events: events}, available, nil
}

func normalizeJob(kind string, input scaleset.JobMessageBase, runnerID int, runnerName, result string) (jobEvent, error) {
	if input.RunnerRequestID < 0 || input.WorkflowRunID <= 0 || !boundedToken(input.RepositoryName, 100) || !boundedToken(input.OwnerName, 100) || !boundedToken(input.JobID, 256) || len(input.RequestLabels) > 32 {
		return jobEvent{}, errors.New("invalid job event")
	}
	for _, label := range input.RequestLabels {
		if !boundedToken(label, 100) {
			return jobEvent{}, errors.New("invalid job label")
		}
	}
	if (runnerName != "" && !boundedToken(runnerName, 100)) || (result != "" && !boundedToken(result, 100)) || runnerID < 0 {
		return jobEvent{}, errors.New("invalid runner event")
	}
	switch kind {
	case "available", "assigned":
		if runnerID != 0 || runnerName != "" || result != "" {
			return jobEvent{}, errors.New("unexpected runner binding")
		}
	case "started":
		if runnerID <= 0 || runnerName == "" || result != "" {
			return jobEvent{}, errors.New("incomplete started event")
		}
	case "completed":
		boundRunner := runnerID > 0 && runnerName != ""
		runnerlessReassignment := runnerID == 0 && runnerName == "" && result == "canceled"
		if result == "" || (!boundRunner && !runnerlessReassignment) {
			return jobEvent{}, errors.New("incomplete completed event")
		}
	default:
		return jobEvent{}, errors.New("unsupported job event")
	}
	requestID := input.RunnerRequestID
	if requestID == 0 {
		if kind == "available" || input.ScaleSetAssignTime.IsZero() {
			return jobEvent{}, errors.New("job is missing its service assignment identity")
		}
		requestID = syntheticRunnerRequestID(input)
	}
	return jobEvent{Kind: kind, RunnerRequestID: requestID, Repository: input.RepositoryName, Owner: input.OwnerName, JobID: input.JobID, WorkflowRunID: input.WorkflowRunID, RequestLabels: append([]string(nil), input.RequestLabels...), RunnerID: runnerID, RunnerName: runnerName, Result: result}, nil
}

// GitHub can directly assign organization Scale Set work while reporting runnerRequestId=0. The
// durable Rust side still needs one stable positive join key across Assigned/Started/Completed.
// Service request IDs are positive signed integers, so the marked digest-derived namespace keeps
// direct assignments distinct in normal operation; the full job evidence remains independently
// validated and makes a theoretical truncated-digest collision fail closed rather than adopt work.
func syntheticRunnerRequestID(input scaleset.JobMessageBase) int64 {
	hasher := sha256.New()
	hasher.Write([]byte(syntheticRunnerRequestDomain))
	var framed [8]byte
	writeField := func(value string) {
		binary.BigEndian.PutUint64(framed[:], uint64(len(value)))
		hasher.Write(framed[:])
		hasher.Write([]byte(value))
	}
	writeField(input.OwnerName)
	writeField(input.RepositoryName)
	writeField(input.JobID)
	binary.BigEndian.PutUint64(framed[:], uint64(input.WorkflowRunID))
	hasher.Write(framed[:])
	writeField(input.ScaleSetAssignTime.UTC().Format(time.RFC3339Nano))
	labels := append([]string(nil), input.RequestLabels...)
	slices.Sort(labels)
	for _, label := range labels {
		writeField(label)
	}
	digest := hasher.Sum(nil)
	value := int64(binary.BigEndian.Uint64(digest[:8]) & uint64(syntheticRunnerRequestMarker-1))
	return syntheticRunnerRequestMarker | value
}

func normalizeRunner(input *scaleset.RunnerReference) *runner {
	return &runner{ID: input.ID, Name: input.Name, ScaleSetID: input.RunnerScaleSetID}
}
