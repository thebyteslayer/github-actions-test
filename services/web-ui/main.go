package main

import (
	"embed"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
)

const (
	bind     string = "127.0.0.1"
	port     int    = 2668
	version  string = "1.0.0"
	protocol int    = 1
)

const banner = `__/\\\\\\________________________________________________/\\\\\\_____/\\\__________________________________________________
 _\////\\\_______________________________________________\////\\\____\/\\\__________________________________________________
  ____\/\\\__________________________________________________\/\\\____\/\\\________________________________________/\\\______
   ____\/\\\________/\\\\\________/\\\\\\\\__/\\\\\\\\\_______\/\\\____\/\\\____________/\\\\\________/\\\\\_____/\\\\\\\\\\\_
    ____\/\\\______/\\\///\\\____/\\\//////__\////////\\\______\/\\\____\/\\\\\\\\\____/\\\///\\\____/\\\///\\\__\////\\\////__
     ____\/\\\_____/\\\__\//\\\__/\\\___________/\\\\\\\\\\_____\/\\\____\/\\\////\\\__/\\\__\//\\\__/\\\__\//\\\____\/\\\______
      ____\/\\\____\//\\\__/\\\__\//\\\_________/\\\/////\\\_____\/\\\____\/\\\__\/\\\_\//\\\__/\\\__\//\\\__/\\\_____\/\\\_/\\__
       __/\\\\\\\\\__\///\\\\\/____\///\\\\\\\\_\//\\\\\\\\/\\__/\\\\\\\\\_\/\\\\\\\\\___\///\\\\\/____\///\\\\\/______\//\\\\\___
        _\/////////_____\/////________\////////___\////////\//__\/////////__\/////////______\/////________\/////_________\/////____`

//go:embed all:.output
var embedded embed.FS

func main() {
	destDir := extractDir()
	cleanupDir := filepath.Dir(destDir)

	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sigs
		fmt.Println("Shutting down...")
		os.RemoveAll(cleanupDir)
		os.Exit(1)
	}()
	defer os.RemoveAll(cleanupDir)

	if err := extract(destDir); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}

	rt, ok := findRuntime()
	if !ok {
		fmt.Fprintln(os.Stderr, "error: no runtime found")
		os.Exit(1)
	}

	printBanner()

	cmd := exec.Command(rt, filepath.Join(destDir, "server", "index.mjs"))
	cmd.Env = append(os.Environ(),
		fmt.Sprintf("PORT=%d", port),
		fmt.Sprintf("HOST=%s", bind),
		fmt.Sprintf("NITRO_PORT=%d", port),
		fmt.Sprintf("NITRO_HOST=%s", bind),
	)

	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			os.Exit(exitErr.ExitCode())
		}
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func printBanner() {
	fmt.Println(banner)
	fmt.Println()
	fmt.Println("Starting localboot WebUI..")
	fmt.Println()
	fmt.Println("Author: ByteSlayer (@thebyteslayer)")
	fmt.Printf("Version: v%s\n", version)
	fmt.Printf("Protocol: v%d\n", protocol)
	fmt.Println()
	fmt.Printf("Listening on %s:%d\n", bind, port)
	fmt.Printf("Access the WebUI at http://localhost:%d\n", port)
	fmt.Println()
}

func extractDir() string {
	if runtime.GOOS == "windows" {
		return filepath.Join(os.TempDir(), "localboot", "web-ui")
	}
	return "/tmp/localboot/web-ui"
}

func extract(destDir string) error {
	return fs.WalkDir(embedded, ".output", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		dest := filepath.Join(destDir, filepath.FromSlash(path[len(".output"):]))
		if d.IsDir() {
			return os.MkdirAll(dest, 0755)
		}
		if err := os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
			return err
		}
		data, err := embedded.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(dest, data, 0644)
	})
}

func findRuntime() (string, bool) {
	for _, name := range []string{"bun", "node"} {
		if path, err := exec.LookPath(name); err == nil {
			return path, true
		}
	}
	return "", false
}
