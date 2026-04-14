# ⚽️ Tip Genius V3.2.0

AI-powered soccer match predictions with a modern, responsive web interface.

## Overview

Tip Genius combines the power of Large Language Models (LLMs) with real-world odds data to generate informed soccer match predictions. The project features a clean, responsive web interface that displays predictions for various soccer leagues including the Premier League, Bundesliga, La Liga and UEFA Champions League.

![Tip Genius iOS PWA](/.github/assets/screenshot_ios_pwa.png)

`Mobile Progressive Web App running on iOS (showing dark mode / predictions / LLM picker)`

## LLM Integration

Tip Genius leverages LLMs to generate match predictions by analyzing odds data and applying domain knowledge. The project uses a lightweight approach with direct API calls instead of individual LLM libraries, minimizing dependencies while maintaining robust performance. This approach ensures sustainable operation while delivering quality predictions.

So far, the following LLM models have been successfully tested with Tip Genius:

- **Mistral Medium 3.1** (Free API, via Mistral AI)
- **OpenAI GPT-5 Mini** (via OpenAI and OpenRouter)
- **DeepSeek Chat V3.2** (via DeepSeek)
- **Meta Llama 4 Maverick** (via DeepInfra)
- **Microsoft Phi-4** (via DeepInfra)
- **Google Gemini 2.5 Flash Lite** (via OpenRouter)
- **xAI Grok 4.1 Fast** (via OpenRouter)

Known LLM Issues:

- Anthropic Claude 4.x (does not have a native JSON mode yet, sometimes struggles to generate valid prediction output)

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
# Install Python dependencies
uv sync

# Install development dependencies
uv sync --group dev

# Install frontend dependencies
npm install

# Build frontend assets for production
npm run build

# Build CSS only
npm run build:css

# Generate build-time version file
npm run generate:version

# Watch CSS changes during development
npm run watch:css

# Run Python linting and formatting checks
uv run ruff check src/
uv run ruff format --check src/

# Run type checking
uv run pyright src/

# Check frontend formatting
npx prettier --check "public/**/*.{js,css,json}"

# Optional local autofix commands
uv run ruff check --fix src/
uv run ruff format src/
npx prettier --write "public/**/*.{js,css,json}" "api/**/*.js" "src/**/*.{yaml,yml}" ".github/**/*.yaml" "utils/**/*.py"

# Install pre-commit hooks
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
```

`npm test` is currently a placeholder and exits with an error; there is no dedicated automated test suite yet.

### Generated Files

- Edit `src/css/styles.css`, not `public/css/tailwind.css`
- Treat `public/version.json` as a generated file created by `npm run generate:version` or `npm run build`

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
   - The `match-predictions.yaml` workflow runs on Tuesdays and Fridays at 03:00 UTC and also supports manual `workflow_dispatch` runs
   - The workflow runs repository quality checks before executing the prediction update job

3. **Environment Configuration**:
   - GitHub Actions uses repository secrets for API and KV credentials
   - GitHub Actions uses repository variables for debug controls such as `DEBUG_MODE` and `DEBUG_PROCESSING_LIMIT`
   - Vercel needs the API route variables used by `api/predictions.js`: `KV_REST_API_URL`, `KV_REST_API_READ_ONLY_TOKEN`, and `KV_DEFAULT_KEY`

4. **Monitoring**:
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

3. Install frontend dependencies:

   ```bash
   npm install
   ```

4. Install Vercel CLI and login:

   ```bash
   npm i -g vercel
   vercel login
   ```

5. Configure environment variables:

   Use the checked-in example file as the starting point:

   ```bash
   cp .env.example .env.local
   ```

   Then edit `.env.local` with your actual credentials and local debug settings. The example file includes the full set of supported variables, including `DEBUG_PROCESSING_LIMIT`.

   For deployment:
   - Add API and KV credentials to GitHub Actions repository secrets
   - Add debug controls such as `DEBUG_MODE` and `DEBUG_PROCESSING_LIMIT` to GitHub Actions repository variables
   - Add the API route variables required by Vercel (`KV_REST_API_URL`, `KV_REST_API_READ_ONLY_TOKEN`, `KV_DEFAULT_KEY`) to Vercel Environment Variables

### Running Locally

1. Start the development server:

   ```bash
   vercel dev
   ```

2. Open <http://localhost:3000> in your browser

If you are changing styles locally, run `npm run watch:css` in a separate terminal so `public/css/tailwind.css` stays in sync.

## Credits

- Team logos originally sourced from [football-logos](https://github.com/luukhopman/football-logos) (thanks for your great work 👍🏻)
- The `utils/process_team_logos.py` script can be used to automatically download and standardize all team logos from the repository

## License

This project is licensed under the **MIT License with Commons Clause**. Free for personal, educational, and research use. Commercial use requires permission.

See the [LICENSE](LICENSE) file for complete terms.

## Links

- [Live Website](https://tip-genius.vercel.app)
- [GitHub Repository](https://github.com/thg-muc/tip-genius)
- [Author's LinkedIn](http://linkedin.com/in/thomas-glanzer)
