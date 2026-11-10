// Display names and logos of every model, shared by the main page and the
// statistics dashboard so a rename only happens here.
//
// - `value`: the provider key used by the workflow, KV and the stats JSON
// - `label`: full name (the main page's model picker, the dashboard on wider screens)
// - `short`: short name (the dashboard on phones and in compact columns)
// - `offered`: shown in the main page's model picker; retired models stay
//   listed with `offered: false` so the dashboard keeps naming their history
//
// Classic script (no module): it sets window.TIP_GENIUS_MODELS before
// main.js and the dashboard module run, both loaded after it.

window.TIP_GENIUS_MODELS = [
  // Offered on the main page, in picker order
  {
    value: 'Deepseek-Chat',
    label: 'DeepSeek V4.1 Flash',
    short: 'DeepSeek',
    logo: '/images/llm-logos/DeepSeek.png',
    offered: true,
  },
  {
    value: 'Google-Gemma-Medium',
    label: 'Gemma 4',
    short: 'Gemma',
    logo: '/images/llm-logos/Google.png',
    offered: true,
  },
  {
    value: 'OpenRouter-GPT-OSS-Large',
    label: 'GPT-OSS 120b',
    short: 'GPT-OSS',
    logo: '/images/llm-logos/OpenAI.png',
    offered: true,
  },
  {
    value: 'Nvidia-Nemotron-Lightning',
    label: 'Nemotron 3.5 Lightning',
    short: 'Nemotron',
    logo: '/images/llm-logos/NVIDIA.png',
    offered: true,
  },
  {
    value: 'Mistral-Ministral',
    label: 'Ministral 14B',
    short: 'Ministral',
    logo: '/images/llm-logos/Mistral.png',
    offered: true,
  },
  {
    value: 'Meta-Llama-Mid',
    label: 'Llama 4 Maverick',
    short: 'Llama',
    logo: '/images/llm-logos/Meta.png',
    offered: true,
  },
  {
    value: 'Microsoft-Phi-Medium',
    label: 'Phi 4 Medium',
    short: 'Phi',
    logo: '/images/llm-logos/Microsoft.png',
    offered: true,
  },

  // Retired or not offered (kept for the statistics and for re-enabling)
  {
    value: 'Google-Gemini-Flash-Lite',
    label: 'Gemini 2.5 Flash Lite',
    short: 'Gemini Lite',
    logo: '/images/llm-logos/Google.png',
    offered: false,
  },
  {
    value: 'OpenRouter-Grok',
    label: 'Grok 4.3',
    short: 'Grok',
    logo: '/images/llm-logos/xAI.png',
    offered: false,
  },
  {
    value: 'OpenRouter-Claude-Haiku',
    label: 'Claude Haiku 4.5',
    short: 'Claude Haiku',
    logo: '/images/llm-logos/Anthropic.png',
    offered: false,
  },
  {
    value: 'Google-Gemini-Flash',
    label: 'Gemini Flash',
    short: 'Gemini',
    logo: '/images/llm-logos/Google.png',
    offered: false,
  },
  {
    value: 'Meta-Llama-70b',
    label: 'Llama 70B',
    short: 'Llama 70B',
    logo: '/images/llm-logos/Meta.png',
    offered: false,
  },
  {
    value: 'Mistral-Large',
    label: 'Mistral Large',
    short: 'Mistral L',
    logo: '/images/llm-logos/Mistral.png',
    offered: false,
  },
  {
    value: 'Mistral-Medium',
    label: 'Mistral Medium',
    short: 'Mistral M',
    logo: '/images/llm-logos/Mistral.png',
    offered: false,
  },
  {
    value: 'OpenAI-GPT-Mini',
    label: 'GPT Mini',
    short: 'GPT Mini',
    logo: '/images/llm-logos/OpenAI.png',
    offered: false,
  },
  {
    value: 'OpenRouter-Gemini-Flash-Lite',
    label: 'Gemini Flash Lite',
    short: 'Gemini Lite',
    logo: '/images/llm-logos/Google.png',
    offered: false,
  },
  {
    value: 'OpenRouter-GPT-Mini',
    label: 'GPT Mini (OpenRouter)',
    short: 'GPT Mini OR',
    logo: '/images/llm-logos/OpenAI.png',
    offered: false,
  },
]
