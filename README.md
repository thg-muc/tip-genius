# ⚽️ Tip Genius

AI-powered soccer match predictions with a modern, responsive web interface.

## Overview

Tip Genius combines the power of Large Language Models (LLMs) with real-world odds data to generate informed soccer match predictions. The project features a clean, responsive web interface that displays predictions for various soccer leagues including the Bundesliga, Premier League, and UEFA Champions League.

## LLM Integration

Tip Genius leverages LLMs to generate match predictions by analyzing odds data and applying domain knowledge. The LLM selection prioritizes cost-effectiveness and reliability, focusing on models that offer free tiers or competitive pricing while maintaining robust performance. This approach ensures sustainable operation while delivering quality predictions.

The following LLMs have been tested with Tip Genius:

- Mistral Large
- Google Gemini
- DeepSeek Chat
- Anthropic Claude

## Features

- 🎯 Match predictions using advanced LLMs (Anthropic, Mistral, DeepSeek)
- 🌐 Clean, responsive web interface with dark mode support
- 📊 Real-time odds data integration
- ⚡️ Fast, serverless architecture using Vercel
- 📱 Mobile-friendly design with PWA support
- 🎨 Modern UI with Tailwind CSS
- 🔄 Automated prediction updates via GitHub Actions
- 💾 Efficient data storage using Vercel KV (Redis)

## Tech Stack

### Frontend

- HTML5 with semantic markup
- Vanilla JavaScript
- Tailwind CSS for styling
- Service Workers for Progessive Web App (PWA) functionality

### Backend

- Python 3.11+ for prediction generation
- Polars for efficient data processing
- Ability to integrate multiple LLM providers
- GitHub Actions for automation

### Infrastructure

- Vercel for hosting and serverless functions
- Vercel KV Store for data persistence
- GitHub for version control and CI/CD

## Local Development

### Prerequisites

- Python 3.11 or higher
- Node.js and npm (latest LTS version)

### Setup

1. Clone the repository:

    ```bash
    git clone https://github.com/thg-muc/tip-genius.git
    cd tip-genius
    ```

2. Set up Python environment (using conda):

    ```bash
    conda env create -f conda.yaml
    conda activate tip-genius
    ```

3. Install dependencies:

    ```bash
    pip install -r requirements.txt
    ```

4. Configure environment variables:

    Create a `.env.local` file in the project root with the following variables:

    ```bash
    ODDS_API_KEY=your_odds_api_key
    ANTHROPIC_API_KEY=your_anthropic_api_key
    MISTRAL_API_KEY=your_mistral_api_key
    DEEPSEEK_API_KEY=your_deepseek_api_key
    KV_REST_API_TOKEN=your_vercel_kv_token
    KV_REST_API_URL=your_vercel_kv_url
    ```

    In case you make a deployment later, these variables should be added to Github Secrets.

### Running Locally

1. Start the development server from the project root:

    ```bash
    vercel dev
    ```

2. Open <http://localhost:3000> in your browser

## Deployment

The project is automatically deployed to Vercel when changes are pushed to the main branch. Manual deployments can be triggered using:

```bash
vercel deploy --prod
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Links

- [Live Website](https://tip-genius.vercel.app)
- [GitHub Repository](https://github.com/thg-muc/tip-genius)
- [Author's LinkedIn](http://linkedin.com/in/thomas-glanzer)
