# ⚽️ Tip Genius V3.0.0

AI-powered soccer match predictions with a modern, responsive web interface.

## Overview

Tip Genius combines the power of Large Language Models (LLMs) with real-world odds data to generate informed soccer match predictions. The project features a clean, responsive web interface that displays predictions for various soccer leagues including the Premier League, Bundesliga, La Liga and UEFA Champions League.

![Tip Genius iOS PWA](/.github/assets/screenshot_ios_pwa.png)

`Mobile Progressive Web App running on iOS (showing dark mode / predictions / LLM picker)`

## LLM Integration

Tip Genius leverages LLMs to generate match predictions by analyzing odds data and applying domain knowledge. The project uses a lightweight approach with direct API calls instead of individual LLM libraries, minimizing dependencies while maintaining robust performance. This approach ensures sustainable operation while delivering quality predictions.

So far, the following LLM families have been successfully tested with Tip Genius:

- Mistral AI (Free API available)
- Google Gemini (Free API available)
- DeepSeek Chat
- OpenAI ChatGPT
- Meta Llama (via DeepInfra)
- Microsoft Phi (via DeepInfra)
- xAI Grok (via OpenRouter)

Issues with some LLMs:

- Anthropic Claude (does not have a native JSON mode yet, sometimes struggles to generate valid prediction output)

## Features

- 🎯 Match predictions using advanced LLMs
- 🌐 Clean, responsive web interface with dark mode support
- 📊 Real-time odds data integration
- 🎮 Team logo integration with fuzzy name matching
- ⚡️ Fast, serverless architecture using Vercel
- 📱 Mobile-friendly design with PWA support
- 🎨 Modern UI with Tailwind CSS
- 🔄 Automated prediction updates via GitHub Actions
- 💾 Efficient data storage using Vercel KV (Redis)
- 🔢 Build-time version generation for deployment tracking

## Tech Stack

### Frontend

- HTML5 with semantic markup
- Vanilla JavaScript
- Tailwind CSS for styling
- Service Workers for Progressive Web App (PWA) functionality
- Smart image caching and preloading system

### Backend

- Python 3.12+ for prediction generation
- Lightweight LLM integration via direct API calls (no heavy LLM libraries required)
- Fuzzy team name matching for logo association
- GitHub Actions for automation

### Infrastructure

- Vercel for hosting and serverless functions
- Vercel KV Store for data persistence
- GitHub for version control and CI/CD

### Dependencies

The project intentionally maintains minimal dependencies:

- `polars`: Fast, parallel data processing
- `PyYAML`: Configuration management
- `requests`: HTTP client for API calls (instead of heavier LLM libraries)
- `python-slugify`: Consistent string normalization

This lightweight approach ensures:

- Easier updates and maintenance
- Faster deployment
- Better performance

## Development Tools

The project uses modern Python and frontend development tools for code quality and consistency:

### Python Development

- **uv**: Ultra-fast Python package manager replacing pip/conda
- **ruff**: Fast Python linter and formatter (replaces black, isort, pylint)
- **pyright**: Static type checker with standard strictness mode
- **pre-commit**: Git hooks for automated quality checks

### Frontend Development

- **prettier**: Code formatter for JavaScript, CSS, JSON, and YAML files
- **Tailwind CSS**: Utility-first CSS framework for styling

### Quality Assurance

- **Pre-commit hooks**: Automated checks for:
  - Python linting and formatting (ruff)
  - Type checking (pyright)
  - Frontend formatting (prettier)
  - Conventional commit message validation
  - File quality checks (trailing whitespace, YAML syntax)

### Development Commands

```bash
# Install dependencies
uv sync

# Install development dependencies
uv sync --group dev

# Run Python linting and formatting
uv run ruff check --fix src/
uv run ruff format src/

# Run type checking
uv run pyright src/

# Format frontend files
npx prettier --write "public/**/*.{js,css,json}"

# Install pre-commit hooks
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
```

## Vercel Setup

Tip Genius is designed to run completely on Vercel's free tier:

1. **Project Configuration**:
   - Create a new project in Vercel and link to your GitHub repository
   - Set up a Vercel KV store (Redis) for data persistence
   - Configure all environment variables in project settings

2. **CI/CD Pipeline**:
   - Automatic deployments triggered by GitHub push events
   - Preview deployments for pull requests
   - GitHub Actions handle prediction generation while Vercel manages deployment

3. **Monitoring**:
   - Access request logs and performance metrics in Vercel dashboard
   - Web Analytics available in free tier

## Local Development

### Prerequisites

- Python 3.12 or higher
- Vercel CLI
- Node.js and npm (latest LTS version)
- Vercel account and KV store setup

### Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/thg-muc/tip-genius.git
   cd tip-genius
   ```

2. Set up Python environment using uv:

   ```bash
   # Install uv (fast Python package manager)
   curl -LsSf https://astral.sh/uv/install.sh | sh

   # Install project dependencies
   uv sync
   ```

3. Install Vercel CLI and login:

   ```bash
   npm i -g vercel
   vercel login
   ```

4. Configure environment variables:

   Create a `.env.local` file in the project root with the following variables:

   ```bash
   # API Keys for Odds Data
   ODDS_API_KEY=your_odds_api_key

   # API Keys for LLM Providers
   ANTHROPIC_API_KEY=your_anthropic_api_key
   DEEPINFRA_API_KEY=your_deepinfra_api_key
   DEEPSEEK_API_KEY=your_deepseek_api_key
   GOOGLE_API_KEY=your_google_api_key
   MISTRAL_API_KEY=your_mistral_api_key
   OPENAI_API_KEY=your_openai_api_key
   OPENROUTER_API_KEY=your_openrouter_api_key

   # Vercel KV (Redis) Configuration
   KV_REST_API_TOKEN=your_vercel_kv_token
   KV_REST_API_URL=your_vercel_kv_url
   ```

   For deployment, these variables should be added to Github Secrets and Vercel Environment Variables.

### Running Locally

1. Start the development server:

   ```bash
   vercel dev
   ```

2. Open <http://localhost:3000> in your browser

## License & Credits

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

- Team logos originally sourced from [football-logos](https://github.com/luukhopman/football-logos) (thanks for your great work 👍🏻)

## Links

- [Live Website](https://tip-genius.vercel.app)
- [GitHub Repository](https://github.com/thg-muc/tip-genius)
- [Author's LinkedIn](http://linkedin.com/in/thomas-glanzer)
