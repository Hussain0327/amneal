// Command proxy is the strangler Step 3 transparent reverse proxy that will
// front the Python API on Fly (docs/GO_PROXY_ROLLOUT.md). No deploy config
// runs it yet; it ships inert.
package main

import (
	"log"
	"os"

	"github.com/Hussain0327/amneal/go/internal/proxy"
)

func main() {
	logger := log.New(os.Stderr, "proxy: ", log.LstdFlags|log.LUTC)
	cfg, err := proxy.ConfigFromEnv()
	if err != nil {
		logger.Fatal(err)
	}
	logger.Printf("listening on %s, upstream %s", cfg.Addr, cfg.Upstream)
	if err := proxy.Run(cfg, logger); err != nil {
		logger.Fatal(err)
	}
}
