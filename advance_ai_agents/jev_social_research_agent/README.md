![Jev × socai social research workflow](assets/workflow.svg)

# Jev Social Research Agent

> Route a social research goal with Jev, collect evidence through the local socai CLI, and return a source-linked Markdown brief.

This advanced-agent example demonstrates a narrow decision boundary between model inference and browser execution. [Jev](https://typesafe.ai/) selects exactly one typed route; [socai](https://github.com/socai-io/socai) performs one read-only search in a browser session it already supports. The Python adapter validates both sides and retains only public evidence fields and platform URLs.

It is a compact reference implementation of the pattern used by [Jev Social](https://github.com/socai-io/jev-social), not a replacement for its multi-step research UI.

## 🚀 Features

- **Typed Jev routing**: Instagram, TikTok, LinkedIn, or an explicit unsupported outcome.
- **Bounded local execution**: fixed `socai <platform> search` command shape, no shell, timeout, result limit, and combined output cap.
- **Credential isolation**: the OpenRouter key is removed from the socai child environment.
- **Evidence contract**: unknown fields, local paths, raw diagnostics, and non-platform URLs are discarded.
- **Auditable report**: source links, comparison table, limitations, route confidence, and measured timing.
- **Offline demo**: deterministic fixture and tests run without an API key, browser, or network.

## 🛠️ Tech Stack

- **Python 3.10+**: standard library only.
- **Jev Decisions API**: typed route selection through OpenRouter.
- **socai CLI**: local-browser social evidence collection.
- **unittest**: deterministic integration and security checks.

## Workflow

```text
research goal
     │
     ▼
Jev choice: instagram_search | tiktok_search | linkedin_search | unsupported
     │                              rejected ───────────────────────┐
     ▼                                                            │
validated platform ──▶ fixed local socai search ──▶ untrusted JSON │
                                                        │          │
                                                        ▼          │
                              URL allowlist + public-field projection
                                                        │
                                                        ▼
                                             source-linked brief
```

The model never writes a command. Its answer is accepted only when it matches the closed route schema and any explicitly selected platform. `subprocess.Popen` receives an argument list directly, and the adapter does not forward the routing credential. The resulting JSON is treated as untrusted data before Markdown rendering.

## 📦 Getting Started

### Prerequisites

- Python 3.10 or newer.
- For the live path: an [OpenRouter API key](https://openrouter.ai/keys), a compatible [socai CLI](https://github.com/socai-io/socai), and a browser session supported by socai.
- No credentials or browser are required for the fixture demo and tests.

### Environment Variables

```bash
cp .env.example .env
set -a && source .env && set +a
```

```env
OPENROUTER_API_KEY=your_openrouter_api_key
OPENROUTER_JEV_MODEL=~typesafe/jev-latest
SOCAI_BIN=socai
```

`OPENROUTER_JEV_MODEL` and `SOCAI_BIN` are optional. The example does not send `OPENROUTER_API_KEY` to the socai process.

### Installation

```bash
git clone https://github.com/Arindam200/awesome-ai-apps.git
cd awesome-ai-apps/advance_ai_agents/jev_social_research_agent
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## ⚙️ Usage

Run the complete offline demo first:

```bash
python main.py "Find emerging AI creator formats on Instagram" --fixture
```

Run a live, bounded search:

```bash
python main.py "Find emerging AI creators on TikTok" --platform auto --limit 4
```

Write the same evidence-only report to disk:

```bash
python main.py "Compare AI creator formats on Instagram" --limit 4 --output report.md
```

The live command prints only the projected report. It does not print the raw socai response, local artifact paths, the full command, or browser diagnostics.

### Test

```bash
python -m unittest discover -s tests -v
```

The tests cover malformed Jev output, explicit-route conflicts, shell-like query text, credential isolation, bounded subprocess output, URL filtering, Markdown neutralization, and the offline end-to-end path.

## Example output

```markdown
# Jev × socai social research brief

**Goal:** Find emerging AI creator formats on Instagram
**Route:** instagram at 91% confidence

## Evidence snapshot

Captured 4 source-linked records. The notes below restate only text and metrics present in the captured evidence.
```

## 📂 Project Structure

```text
jev_social_research_agent/
├── assets/workflow.svg
├── fixtures/sample_socai.json
├── tests/test_main.py
├── .env.example
├── main.py
├── README.md
└── requirements.txt
```

## Safety and limits

- This example exposes search only. It does not publish, like, follow, message, or change a remote account.
- A successful command does not imply representative coverage. Empty, partial, login-gated, challenged, and rate-limited outcomes stay explicit.
- Platform responses and browser behavior can change. Review source posts before consequential use.
- Use a browser profile appropriate for research and follow each platform policy.

## 🤝 Contributing

Contributions are welcome. Please follow the repository [contribution guide](../../CONTRIBUTING.md).

## 📄 License

This example is distributed under the repository MIT license.

## 🙏 Acknowledgments

- [Jev by TypeSafe](https://typesafe.ai/) for typed decision routing.
- [socai](https://github.com/socai-io/socai) for local-browser social workflows.
- [Jev Social](https://github.com/socai-io/jev-social) for the browser-grounded research pattern.
