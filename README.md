# my_agent

`my_agent` is a local-first agent backend built with `LangGraph`. It provides a structured execution flow and integrates retrieval, memory, web tools, session management, and sandboxed code execution.

## Features

- LangGraph-based agent workflow (`planner -> executor -> tool -> finalizer`)
- Local RAG pipeline with MarkItDown, chunking, and Qdrant
- Oracle-backed memory (`conversation_memory`, `summary_memory`, `persona_memory`)
- MCP-based web search and page fetching
- Session-scoped filesystem access
- OpenSandbox + Docker based terminal execution
- LangSmith tracing support

## Project Structure

- `chat_sessions.py`: local CLI entry point
- `backend/agent`: agent orchestration
- `backend/rag`: retrieval and indexing
- `backend/memory`: Oracle-backed memory
- `backend/tools`: tool registry and tool implementations
- `backend/session`: local session storage
- `backend/sandbox`: sandbox integration
- `backend/config`: JSON configuration

## Requirements

- Python 3.11 or 3.12 recommended
- CUDA-capable GPU recommended for embedding models
- Docker required for sandbox terminal execution
- Oracle Database Free required for persistent memory

## Run

Start the main program:

```powershell
python chat_sessions.py
```

If terminal tools are required, start OpenSandbox first:

```powershell
opensandbox-server --config backend/sandbox/runtime/opensandbox.server.toml
```

## Configuration

Primary configuration files:

- `backend/config/llm.json`
- `backend/config/rag.json`
- `backend/config/memory.json`
- `backend/config/sandbox.json`
- `backend/config/mcp/*.json`

Secrets should be placed in `.env`. A sample file is provided in `.env.example`.

## Local Assets Not Included in This Repository

The following files are intentionally not included and should be prepared locally:

- `.env`: place at the repository root
- RAG embedding model: `backend/rag/models/embedding/`
- RAG reranker model: `backend/rag/models/reranker/`
- Memory embedding model: `backend/memory/models/embedding/`
- Optional source documents for ingestion: `raw_data/`
- Runtime workspace: `agent_workspace/`
- Local Qdrant storage: `backend/rag/storage/`
- Sandbox runtime files: `backend/sandbox/runtime/`

These local artifacts should not be committed.
