package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"
)

// waitSignal 阻塞到收到 SIGINT / SIGTERM。
func waitSignal() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
}
