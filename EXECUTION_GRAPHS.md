# Workflow execution graphs

These DAGs describe the execution shape exercised by the inputs in
`WORKFLOW_INPUTS.txt`. Node labels use the four profiling types:

- **LLM** — llama.cpp chat/completion request
- **Tool** — browser, search, code, or application tool invocation
- **Control** — workflow loop, planner, router, or cache decision
- **Data** — task, prompt, tool result, repository, or report data

## Browser Use

```mermaid
flowchart LR
  D1[(Data: browser task)] --> C1{Control: Agent step loop}
  C1 --> L1[LLM: llama.cpp]
  L1 --> T1([Tool: Playwright browser])
  T1 --> D2[(Data: page title)]
  D2 --> C1
  C1 --> D3[(Data: final answer)]
```

## GPT Researcher

```mermaid
flowchart LR
  D1[(Data: research question)] --> C1{Control: research planner}
  C1 --> T1([Tool: DuckDuckGo/search])
  T1 --> D2[(Data: scraped sources)]
  D2 --> C1
  C1 --> L1[LLM: llama.cpp]
  L1 --> C2{Control: report writer}
  C2 --> D3[(Data: research report)]
```

## Speculative Tools

```mermaid
flowchart LR
  D1[(Data: user task)] --> L1[LLM: llama.cpp]
  L1 --> C1{Control: adapter loop}
  C1 --> C2{Control: prediction/cache check}
  C2 --> T1([Tool: weather lookup])
  T1 --> D2[(Data: tool result/cache)]
  D2 --> C1
  C1 --> D3[(Data: final response and stats)]
```

## AutoGPT

```mermaid
flowchart LR
  D1[(Data: objective)] --> C1{Control: AutoGPT loop}
  C1 --> L1[LLM: llama.cpp]
  L1 --> C2{Control: permission/action router}
  C2 --> T1([Tool: search/filesystem/actions])
  T1 --> D2[(Data: observations)]
  D2 --> C1
  C1 --> D3[(Data: checklist/result)]
```

## MetaGPT

```mermaid
flowchart LR
  D1[(Data: project idea)] --> C1{Control: Team/role orchestration}
  C1 --> L1[LLM: llama.cpp]
  L1 --> C1
  C1 --> T1([Tool: file/code generation])
  T1 --> D2[(Data: project files/specification)]
  D2 --> C1
  C1 --> D3[(Data: design summary)]
```

## SWE-agent

```mermaid
flowchart LR
  D1[(Data: repository + problem statement)] --> C1{Control: agent trajectory loop}
  C1 --> L1[LLM: llama.cpp]
  L1 --> C2{Control: action parser/permission}
  C2 --> T1([Tool: shell/editor/repository])
  T1 --> D2[(Data: files, diffs, command output)]
  D2 --> C1
  C1 --> D3[(Data: patch/summary)]
```

## OpenHands Agent Canvas

```mermaid
flowchart LR
  D1[(Data: UI task)] --> C1{Control: Agent Canvas session}
  C1 --> L1[LLM: llama.cpp]
  L1 --> C2{Control: agent-server router}
  C2 --> T1([Tool: workspace/browser/shell])
  T1 --> D2[(Data: tool observations)]
  D2 --> C1
  C1 --> D3[(Data: UI response/artifacts)]
```

## Execution evidence

The dedicated runs produced profiled traces under `traces/`. Browser Use,
Speculative Tools, and MetaGPT completed successfully; GPT Researcher reached
report writing before its test timeout, AutoGPT reached its interactive agent
loop but exited on a terminal permission prompt, SWE-agent reached startup but
needs a clean per-run tool bundle, and OpenHands is intentionally long-running
as a UI service.
