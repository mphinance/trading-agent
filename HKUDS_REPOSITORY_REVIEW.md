# HKUDS Repository README Review — Extended Top 12

> **Source:** [HKUDS on GitHub](https://github.com/HKUDS)  
> **Ranking basis:** A practical “top projects” list based on apparent GitHub visibility, ecosystem importance, README prominence, and project scope—not a precise live star-count ranking. HKUDS has roughly 90+ public repositories, and popularity/activity can change quickly.

## Quick ranking

| Rank | Repository | Category | Short description |
|---:|---|---|---|
| 1 | [LightRAG](https://github.com/HKUDS/LightRAG) | RAG framework | Graph-enhanced retrieval-augmented generation |
| 2 | [nanobot](https://github.com/HKUDS/nanobot) | Agent runtime | Lightweight self-hosted personal AI agent |
| 3 | [DeepTutor](https://github.com/HKUDS/DeepTutor) | AI application | Personalized tutoring and research workspace |
| 4 | [CLI-Anything](https://github.com/HKUDS/CLI-Anything) | Agent tooling | Makes existing software controllable by agents |
| 5 | [ClawTeam](https://github.com/HKUDS/ClawTeam) | Agent orchestration | Multi-agent swarm coordination |
| 6 | [AI-Trader](https://github.com/HKUDS/AI-Trader) | Trading platform | Agent-native trading and signal-sharing platform |
| 7 | [RAG-Anything](https://github.com/HKUDS/RAG-Anything) | Multimodal RAG | RAG for text, images, tables, equations, and documents |
| 8 | [VideoRAG](https://github.com/HKUDS/VideoRAG) | Video understanding | Long-context video retrieval and question answering |
| 9 | [AnyGraph](https://github.com/HKUDS/AnyGraph) | Graph ML | Zero-shot graph foundation model |
| 10 | [MiniRAG](https://github.com/HKUDS/MiniRAG) | Lightweight RAG | Small-model, graph-assisted RAG |
| 11 | [LLMRec](https://github.com/HKUDS/LLMRec) | Recommendation systems | LLM-based graph augmentation for recommendations |
| 12 | [OpenSpace](https://github.com/HKUDS/OpenSpace) | Agent tooling | Skill-management layer for AI agents |

---

# 1. [LightRAG](https://github.com/HKUDS/LightRAG)

**Category:** Retrieval-augmented generation, knowledge graphs  
**Paper:** EMNLP 2025

## README summary

LightRAG is HKUDS's flagship RAG framework. It combines conventional vector retrieval with graph-based representations to improve knowledge discovery, context coherence, and retrieval across connected concepts.

The project has grown from a research implementation into a deployable RAG server with a Web UI, multiple storage backends, authentication options, multimodal support, evaluation, and tracing.

## Main capabilities

- Knowledge-graph-enhanced RAG
- Vector and graph retrieval
- Document ingestion and deletion
- Citation support
- Web UI
- API server
- Reranking
- Multiple chunking strategies
- Multimodal RAG through RAG-Anything
- Support for text, images, tables, equations, and Office documents
- Multiple LLM and embedding providers
- PostgreSQL, MongoDB, Neo4j, OpenSearch, and other storage backends
- Docker deployment
- RAGAS evaluation
- Langfuse tracing
- Offline and air-gapped deployment options

## Installation

The README recommends `uv`:

```bash
uv tool install "lightrag-hku[api]"
```

Source and Docker Compose installation paths are also documented.

## Overall impression

LightRAG is probably HKUDS's most important infrastructure project. Several other repositories—particularly MiniRAG, RAG-Anything, VideoRAG, and DeepTutor—either build on its ideas or integrate with it.

**Best suited for:** Production-oriented RAG systems, knowledge bases, document search, and graph-enhanced retrieval experiments.

> **Operational note:** The README warns that the server binds to `0.0.0.0` by default and must be protected with authentication or rebound to localhost before network exposure.

---

# 2. [nanobot](https://github.com/HKUDS/nanobot)

**Category:** Personal AI agents

## README summary

`nanobot` is a small, self-hosted Python agent runtime with a browser UI, terminal client, chat integrations, tools, memory, MCP support, model routing, subagents, and scheduled automation.

## Main capabilities

- Web UI and terminal UI
- Telegram, Discord, Slack, WeChat, email, Mattermost, and other chat channels
- File, shell, web, MCP, cron, and image-generation tools
- Long-term memory
- Multi-agent delegation
- OpenAI-compatible API
- Model fallbacks and routing
- Background gateway mode
- Local and hosted model support

## Installation

The README supports:

- macOS/Linux and Windows install scripts
- PyPI
- `uv`
- Source installation
- Python 3.11+

Typical commands include:

```bash
nanobot webui
nanobot gateway
nanobot -m "Hello!"
```

## Overall impression

This is HKUDS's general-purpose agent runtime: relatively small, self-hostable, and intended to serve as a base for personal assistants and automated workflows.

It is the individual-agent counterpart to ClawTeam's swarm layer.

**Best suited for:** Personal AI assistants, automation gateways, chat-connected agents, and agent experimentation.

---

# 3. [DeepTutor](https://github.com/HKUDS/DeepTutor)

**Category:** Personalized education, research, knowledge management

## README summary

DeepTutor is an AI learning and research environment combining tutoring, document understanding, guided learning, knowledge bases, research agents, books, quizzes, memory, coding agents, and partner agents.

## Main capabilities

- Personalized tutoring
- Guided learning and mastery paths
- Deep research and problem-solving
- Question generation and question banks
- Document ingestion
- GraphRAG, LightRAG, PageIndex, and FAISS integrations
- PDF, EPUB, DOCX, XLSX, Markdown, and other formats
- Immersive reading with citations
- Book generation and annotation
- YouTube learning
- External partner agents
- Claude Code and Codex integrations
- MCP services and plugins
- Multiple search and model providers
- Multi-user deployment options

## Overall impression

DeepTutor is one of the most ambitious application-level repositories in the organization. It appears to combine an AI tutor, research assistant, knowledge-management system, and agent platform into a single product.

The frequent release history suggests active, ongoing product development rather than a static paper artifact.

**Best suited for:** AI-assisted education, research workflows, document-heavy knowledge work, and personalized learning products.

---

# 4. [CLI-Anything](https://github.com/HKUDS/CLI-Anything)

**Category:** Agent interoperability and software automation

## README summary

CLI-Anything aims to make existing software agent-native by generating structured command-line interfaces that AI agents can operate.

Rather than teaching an agent how to click through arbitrary GUIs, the project creates a predictable command surface for applications such as design tools, office software, video editors, CAD systems, browser tools, and knowledge-management applications.

## Main capabilities

- Generate CLI harnesses for existing software
- Agent-oriented commands
- Artifact creation and editing
- Preview and live-preview workflows
- Community registry
- CLI-Hub package manager
- Agent skill documentation
- Support for Claude Code, Cursor, Codex, OpenClaw, nanobot, and similar systems
- Testing and end-to-end validation

## Examples

The ecosystem includes harnesses or integrations for:

- Blender
- FreeCAD
- QGIS
- Krita
- Inkscape
- LibreOffice
- Calibre
- Zotero
- Obsidian
- Joplin
- Kdenlive
- Shotcut
- Godot
- Unreal tools
- n8n
- Dify
- Exa
- WireMock
- VideoCaptioner
- Browser automation tools

## Installation

```bash
pip install cli-anything-hub
cli-hub install <name>
```

## Overall impression

CLI-Anything is less a single application than an ecosystem and distribution mechanism for agent-controllable software.

Its core thesis is compelling: reliable agent automation needs explicit, composable interfaces rather than fragile GUI interaction.

**Best suited for:** Agent tool builders, automation engineers, and developers exposing desktop or specialized software to AI agents.

---

# 5. [ClawTeam](https://github.com/HKUDS/ClawTeam)

**Category:** Multi-agent orchestration

## README summary

ClawTeam lets agents form teams. A leader agent can spawn workers, assign tasks, manage dependencies, coordinate via messages, isolate work in Git worktrees, and monitor progress through tmux or a Web UI.

## Main capabilities

- Leader/worker agent architecture
- Agent spawning
- Task dependencies
- Inter-agent messaging
- Git worktree isolation
- tmux-based monitoring
- Web dashboard
- P2P transport support
- Team templates
- Multi-user workflows
- Compatibility with several CLI agents

## Example workflows

### Software engineering

A leader can distribute:

- API design
- Authentication
- Database work
- Frontend development
- Testing

### Machine-learning research

Multiple agents can explore separate experiment directions across GPUs and periodically share results.

### Trading research

The README presents an example team with portfolio management, value, growth, technical, fundamental, sentiment, and risk agents.

## Installation

```bash
pip install clawteam
```

The project also documents source installation and optional transport dependencies.

## Overall impression

ClawTeam is the coordination layer for HKUDS's agent ecosystem:

```text
nanobot       = one agent runtime
CLI-Anything  = tools for agents
ClawTeam      = teams of agents
```

The orchestration primitives are interesting even where the README's ambitious domain examples should be treated as demonstrations rather than validated autonomous systems.

**Best suited for:** Parallel coding, ML experimentation, research teams, and multi-agent workflows.

---

# 6. [AI-Trader](https://github.com/HKUDS/AI-Trader)

**Category:** Agent-native trading platform

## Where does AI-Trader rank?

**Placement: #6 overall in this practical top-12 list.**

That is not because it is necessarily the sixth-most-starred repository. It ranks highly because it is a substantial platform, is closely aligned with HKUDS's newer agent ecosystem, and connects several of their themes:

- AI agents
- Skills
- Social collaboration
- Automated experimentation
- Market data
- Paper trading
- Copy trading
- Broker synchronization

## README summary

AI-Trader is presented as an “agent-native trading platform” where AI agents can join, publish strategies and signals, debate ideas, copy successful traders, synchronize broker activity, and participate in a shared trading community.

The README frames it as a trading platform designed specifically for agents rather than a conventional broker terminal.

## Main capabilities

- Agent registration through a skill file
- Agent-generated trading signals
- Strategy publishing
- Agent discussions and collaboration
- Signal synchronization
- Copy trading
- Broker synchronization
- Stocks, crypto, forex, options, and futures
- Paper trading
- Real market data
- Polymarket paper trading
- Leaderboards and rewards
- Human-facing trading dashboard
- PostgreSQL or SQLite backend
- FastAPI backend
- React frontend
- OpenAPI specifications
- Agent-specific skills

## Agent onboarding

The README's primary agent workflow is:

```text
Read the AI-Trader skill file
Register the agent
Publish signals, strategies, or discussions
Follow/copy other agents
Synchronize trades or broker activity
```

It specifically supports agents such as:

- OpenClaw
- nanobot
- Claude Code
- Codex
- Cursor

## Architecture

```text
AI-Trader
├── skills/       Agent skill definitions
├── docs/api/     OpenAPI specifications
├── service/
│   ├── server/   FastAPI backend
│   └── frontend/ React frontend
└── assets/
```

## Self-hosting

The README supports two database modes:

- PostgreSQL for shared or production deployments
- SQLite for local quick starts

## Overall impression

AI-Trader is not simply a backtesting library or a broker connector. It is closer to an **agent trading social network plus paper-trading and signal infrastructure**.

The unusual idea is that agents are first-class participants. They can publish predictions, exchange ideas, gain followers, and copy one another. This makes it conceptually similar to a social trading platform, except the intended users include autonomous agents.

## Important distinction

The README says “fully automated,” “copy trading,” and “trade across” multiple asset classes. Those claims should not automatically be interpreted as proof that every supported live-trading path is production-safe or that all broker integrations are equally complete.

Before connecting real credentials, inspect separately:

- Authentication and authorization
- Order execution code
- Broker adapter implementations
- Position and risk controls
- Paper/live mode separation
- Copy-trading safeguards
- Credential handling
- API exposure and deployment defaults
- Whether signals are advisory or directly executable

**Best suited for:** Agent trading experiments, paper trading, signal communities, copy-trading prototypes, and research into multi-agent market behavior.

---

# 7. [RAG-Anything](https://github.com/HKUDS/RAG-Anything)

**Category:** Multimodal document RAG

## README summary

RAG-Anything is an all-in-one multimodal retrieval framework built on LightRAG. It is designed for documents containing mixed content—text, images, tables, charts, equations, and other non-text elements.

## Main capabilities

- PDF and Office-document processing
- Image understanding
- Table interpretation
- Mathematical-expression parsing
- Multimodal knowledge graphs
- Cross-modal relationship extraction
- Hybrid vector and graph retrieval
- VLM-enhanced query mode
- MinerU integration
- Direct insertion of pre-parsed content
- Extensible modality handlers
- Context-aware multimodal answers

## Processing pipeline

```text
Document parsing
      ↓
Content analysis
      ↓
Multimodal knowledge graph
      ↓
Hybrid retrieval
      ↓
Question answering
```

## Overall impression

RAG-Anything addresses a major weakness of text-only RAG systems: real documents are not just paragraphs. Technical and business documents often rely on diagrams, tables, equations, screenshots, and layout.

This repository is effectively the multimodal extension of LightRAG.

**Best suited for:** Research papers, financial reports, technical documentation, enterprise knowledge bases, and multimodal document assistants.

---

# 8. [VideoRAG](https://github.com/HKUDS/VideoRAG)

**Category:** Long-context video understanding  
**Associated product:** Vimo Desktop

## README summary

VideoRAG provides a retrieval-augmented approach to understanding very long videos. Its associated Vimo application lets users upload videos and ask questions about them conversationally.

The README claims support for videos ranging from short clips to hundreds of hours.

## Main capabilities

- Video question answering
- Long-video processing
- Multi-video analysis
- Visual and audio understanding
- Scene and moment retrieval
- Graph-driven video indexing
- Hierarchical temporal context
- Cross-video understanding
- Desktop application direction
- LongerVideos benchmark

## Technical approach

The README describes:

- Graph-driven knowledge indexing
- Hierarchical context encoding
- Adaptive retrieval
- Multimodal alignment between queries and video/audio content

## Benchmark

The LongerVideos benchmark is described as containing approximately:

- 164 videos
- 134.6 hours of content
- 602 queries
- Lectures, documentaries, and entertainment

The README reports a VideoRAG score of 60.2% on a listed Video-MME long-video comparison.

## Overall impression

VideoRAG extends HKUDS's graph-RAG direction into video. The core challenge is reducing extremely long video streams into searchable representations without losing temporal, visual, or audio context.

The Vimo desktop application makes the research more accessible, while the underlying VideoRAG algorithm remains relevant to researchers.

**Best suited for:** Video search, lecture analysis, documentary analysis, media archives, and long-context multimodal research.

---

# 9. [AnyGraph](https://github.com/HKUDS/AnyGraph)

**Category:** Graph foundation models  
**Paper:** “AnyGraph: Graph Foundation Model in the Wild”

## README summary

AnyGraph is a graph foundation model intended to support zero-shot predictions across graph domains with different structural and feature distributions.

## Main ideas

- Structural heterogeneity handling
- Feature heterogeneity handling
- Zero-shot graph prediction
- Fast adaptation to new datasets
- Graph mixture-of-experts
- Lightweight expert routing
- Scaling-law analysis
- Link prediction
- Node classification

## Evaluation

The README describes evaluation across 38 graph datasets, including:

- E-commerce
- Academic citation
- Social networks
- Proteins
- Roads
- Email
- Recommendation-style graphs
- Other link-prediction datasets

## Requirements

The documented environment includes:

```text
Python 3.10.13
PyTorch 1.13.0
NumPy 1.23.4
SciPy 1.9.3
```

A 24 GB GPU is recommended for training and testing.

## Overall impression

AnyGraph is a focused academic project. Its significance comes from attempting to make graph models transferable across domains, rather than requiring a separately engineered model for each graph dataset.

**Best suited for:** Graph ML research, link prediction, recommendation research, and cross-domain graph transfer.

---

# 10. [MiniRAG](https://github.com/HKUDS/MiniRAG)

**Category:** Lightweight RAG for small models  
**Paper:** ACL 2026 listing in the README

## README summary

MiniRAG is designed to make RAG work with smaller, open-source language models. It uses heterogeneous graph indexing and lightweight topology-aware retrieval to compensate for weaker semantic capabilities in small models.

## Main contributions

- Heterogeneous graph indexing
- Text-chunk and entity integration
- Lightweight graph retrieval
- Small-model support
- On-device or resource-constrained RAG
- Support for multiple graph databases
- API and Docker deployment
- LiHua-World benchmark dataset

## Reported results

The README claims that MiniRAG:

- Achieves competitive performance with small models
- Uses approximately 25% of the storage space of comparable approaches
- Performs well on single-hop, multi-hop, and summarization tasks

## Installation

```bash
pip install -e .
```

Or via the related LightRAG package:

```bash
pip install lightrag-hku
```

## Overall impression

MiniRAG is the lightweight branch of HKUDS's RAG research. Instead of assuming access to the largest models, it focuses on using graph structure to make smaller models more useful.

**Best suited for:** Local AI, edge/on-device RAG, small-model deployments, and resource-constrained environments.

---

# 11. [LLMRec](https://github.com/HKUDS/LLMRec)

**Category:** Recommendation systems  
**Paper:** WSDM 2024 Oral

## README summary

LLMRec uses large language models to augment user-item recommendation graphs. It applies LLMs to infer additional interaction signals, generate user profiles, and enrich item metadata.

## Three main augmentation strategies

1. Reinforce user-item interaction edges
2. Enhance item attributes
3. Generate user profiles

## Workflow

### Stage 1: LLM augmentation

The project generates:

- Implicit preference signals
- User profiles
- Item attributes
- LLM-enhanced embeddings

### Stage 2: Recommender training

The augmented data is used by the recommendation model for datasets such as:

- Netflix
- MovieLens

## Data

The README discusses:

- User-item interactions
- Textual item information
- Movie posters
- LLM-generated textual metadata
- Visual and text embeddings
- Candidate item generation

## Overall impression

LLMRec is an early and focused example of using an LLM as a data-augmentation component inside a recommender system.

Rather than asking the LLM to directly recommend everything, it uses the model to improve the graph and metadata consumed by a more conventional recommender.

**Best suited for:** Recommender-system research, multimodal recommendation, and LLM-assisted graph augmentation.

---

# 12. [OpenSpace](https://github.com/HKUDS/OpenSpace)

**Category:** Agent skills and capability management

## README summary

OpenSpace is described in the available repository metadata as a skill-management layer for AI agents.

It appears to sit near the boundary between:

- Agent capability discovery
- Skill installation
- Workspace management
- Reusable agent workflows
- Tool and instruction organization

## Likely role in the HKUDS ecosystem

OpenSpace appears to complement:

- `nanobot`, which runs agents
- `CLI-Anything`, which exposes tools
- `ClawTeam`, which coordinates agents
- `DeepTutor`, which consumes skills and agent capabilities

## Overall impression

OpenSpace is less clearly documented in the available README extract than the other projects in this list, so its exact role should be treated as provisional. It is nevertheless relevant because HKUDS increasingly treats skills as installable, reusable units of agent behavior rather than embedding every capability directly into one application.

**Best suited for:** Agent skill catalogs, reusable workflows, and capability management.

---

# How the Projects Fit Together

## Agent ecosystem

```text
OpenSpace
   ↓
Skill and capability management

CLI-Anything
   ↓
Agent-controllable software interfaces

nanobot
   ↓
Individual agent runtime

ClawTeam
   ↓
Multi-agent coordination

DeepTutor / AI-Trader
   ↓
Domain applications built around agents
```

## RAG and knowledge ecosystem

```text
LightRAG
   ↓
Core graph-enhanced RAG

MiniRAG
   ↓
Small-model and lightweight RAG

RAG-Anything
   ↓
Multimodal document RAG

VideoRAG
   ↓
Long-context video RAG

DeepTutor
   ↓
End-user learning and research application
```

## Research lineage

```text
AnyGraph
   ↓
Generalized graph representation and transfer

LLMRec
   ↓
LLM-enhanced recommendation graphs

LightRAG
   ↓
Graph-enhanced retrieval

MiniRAG / RAG-Anything / VideoRAG
   ↓
Specialized RAG extensions
```

# Final Assessment of AI-Trader

AI-Trader is **not an obscure side repository**. In the context of HKUDS's current direction, it is one of the more strategically interesting projects.

I would classify it as:

> **A social, collaborative, agent-native trading platform with paper-trading, signal publishing, copy trading, and broker synchronization—not merely a trading bot.**

Its strongest conceptual connection is to `ClawTeam` and `nanobot`:

- `nanobot` gives an agent a runtime.
- `ClawTeam` lets agents collaborate.
- `AI-Trader` gives those agents a market-facing social and trading environment.

The key caution is that its README describes platform capabilities at a high level. Anyone considering real-money usage should inspect the execution, permissions, credential, and risk-control code separately rather than treating the README as a safety certification.
