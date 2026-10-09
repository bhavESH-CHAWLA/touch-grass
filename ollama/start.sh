#!/bin/sh
# Start Ollama, make sure the model is present (cached on the persistent disk), then keep running.
ollama serve &
sleep 6
ollama pull "${MODEL:-qwen2.5:1.5b}"
wait
