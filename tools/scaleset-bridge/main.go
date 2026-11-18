package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/signal"
)

const maxProtocolLineBytes = 128 * 1024

func main() {
	if len(os.Args) != 1 {
		os.Exit(2)
	}
	// The bridge deliberately ignores ambient proxy, credential, and GitHub configuration. All
	// authority arrives through the private stdin protocol from SmolRunner.
	os.Clearenv()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()

	server := newServer(newOfficialBackend)
	defer server.close()
	if err := serve(ctx, os.Stdin, os.Stdout, server); err != nil {
		os.Exit(1)
	}
}

func serve(ctx context.Context, input io.Reader, output io.Writer, server *server) error {
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), maxProtocolLineBytes)
	encoder := json.NewEncoder(output)
	encoder.SetEscapeHTML(true)

	for scanner.Scan() {
		line := append([]byte(nil), scanner.Bytes()...)
		request, err := decodeRequest(line)
		for index := range line {
			line[index] = 0
		}
		if err != nil {
			if encodeErr := encodeResponse(encoder, errorResponse("invalid_request")); encodeErr != nil {
				return encodeErr
			}
			continue
		}

		response := server.handle(ctx, request)
		request.Start.clearSecrets()
		if err := encodeResponse(encoder, response); err != nil {
			return err
		}
	}
	if err := scanner.Err(); err != nil {
		return err
	}
	return nil
}

func encodeResponse(encoder *json.Encoder, response protocolResponse) error {
	if !responseFitsProtocolLine(response) {
		response = errorResponse("response_too_large")
	}
	return encoder.Encode(response)
}

func responseFitsProtocolLine(response protocolResponse) bool {
	encoded, err := json.Marshal(response)
	return err == nil && len(encoded)+1 <= maxProtocolLineBytes
}

func decodeRequest(line []byte) (request protocolRequest, err error) {
	decoder := json.NewDecoder(bytes.NewReader(line))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		return protocolRequest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return protocolRequest{}, errors.New("trailing protocol value")
	}
	if request.Version != protocolVersion || request.Operation == "" {
		return protocolRequest{}, errors.New("unsupported protocol request")
	}
	return request, nil
}
