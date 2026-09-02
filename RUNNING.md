# Running the workflows

Set `OPENAI_API_KEY` in the shell or `.env`, then run:

```sh
./run.py browser-use "Open example.com and return its title"
./run.py gpt-researcher "Research a small topic"
./run.py metagpt "Create a command-line calculator"
./run.py swe-agent "Fix the failing test"
./run.py autogpt "Complete a small task"
./run.py openhands "start"
```

All workflows default to `gpt-5-nano`, currently OpenAI's cheapest GPT-5 API model. GPT Researcher uses DuckDuckGo and local embeddings to avoid additional API services.

AutoGPT is interactive and requests task/tool approval. OpenHands starts Agent Canvas at `http://localhost:8000`; its task is submitted through the UI. The smoke script limits each launcher to five minutes.

SWE-agent uses `swe-smoke-target/`, generated locally by the runner, so smoke tests do not modify the framework checkout.

For completely local inference, download a GGUF and run `./run-all-local-llama.sh`. It builds and starts `llama.cpp` automatically and routes all six workflows to its local OpenAI-compatible server. No OpenAI API key is used in this mode.
